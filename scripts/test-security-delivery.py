"""Offline tests for the exact Terraform-packaged security delivery observer.

No AWS calls: fake clients expose only the observer's narrow read/metric methods.
Run on a development server: python3 -S -B scripts/test-security-delivery.py
The loader injects SDK stubs, so site-packages and AWS credentials are unnecessary.
"""
import concurrent.futures
import contextlib
import copy
import datetime
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = (ROOT / "security-monitoring.tf").read_text()
REGIONAL = (ROOT / "modules/security-region/main.tf").read_text()
SOURCE = textwrap.dedent(re.search(
    r"security_delivery_watchdog = <<-PYTHON\n(.*?)^  PYTHON$", TERRAFORM, re.M | re.S
).group(1))
REGIONS = re.findall(r'"([a-z]{2}-[a-z]+-\d)"', re.search(
    r"security_delivery_regions = \[(.*?)\n  \]", TERRAFORM, re.S
).group(1))
MAIN, STAGING = "111111111111", "222222222222"
SNS_TOPIC = "arn:aws:sns:us-west-1:" + MAIN + ":cost-alerts"
NOW = datetime.datetime(2026, 9, 7, 12, tzinfo=datetime.timezone.utc)
COUNTERS = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"]
REGIONAL_RULES = ("security-forward-findings", "security-forward-audit")
GUARDDUTY_BASE_FEATURES = {"S3_DATA_EVENTS", "EKS_AUDIT_LOGS", "EBS_MALWARE_PROTECTION",
                         "RDS_LOGIN_EVENTS", "LAMBDA_NETWORK_LOGS", "RUNTIME_MONITORING", "AI_PROTECTION"}
# AWS GuardDuty Investigation preview availability, reviewed 2026-09-08:
# https://docs.aws.amazon.com/guardduty/latest/ug/guardduty-investigation.html
GUARDDUTY_ANALYST_REGIONS = {"us-east-1", "us-east-2", "us-west-2", "ca-central-1",
                           "eu-central-1", "eu-north-1", "eu-west-1", "eu-west-2", "eu-west-3", "ap-northeast-1"}
# Independently reviewed fixtures. The optional native-HCL test below compares
# these to the exact Terraform map used by both EventBridge and the watchdog.
API_DETAILS = [
    {"eventSource": ["cloudtrail.amazonaws.com", "guardduty.amazonaws.com", "config.amazonaws.com", "securityhub.amazonaws.com", "access-analyzer.amazonaws.com"],
     "eventName": ["StopLogging", "DeleteTrail", "UpdateTrail", "PutEventSelectors", "DeleteDetector", "UpdateDetector", "StopConfigurationRecorder", "DeleteConfigurationRecorder", "DisableSecurityHub", "DeleteAnalyzer"]},
    {"eventSource": ["iam.amazonaws.com"],
     "eventName": ["CreateUser", "CreateAccessKey", "CreateLoginProfile", "UpdateLoginProfile", "AddUserToGroup", "AttachUserPolicy", "AttachRolePolicy", "AttachGroupPolicy", "PutUserPolicy", "PutRolePolicy", "PutGroupPolicy", "UpdateAssumeRolePolicy", "DeleteRolePermissionsBoundary", "DeleteUserPermissionsBoundary"]},
    {"eventSource": ["kms.amazonaws.com"],
     "eventName": ["DisableKey", "ScheduleKeyDeletion", "PutKeyPolicy", "DisableKeyRotation"]},
    {"eventSource": ["backup.amazonaws.com"],
     "eventName": ["DeleteBackupVault", "DeleteBackupPlan", "DeleteBackupSelection", "UpdateBackupPlan", "PutBackupVaultAccessPolicy", "DeleteBackupVaultAccessPolicy", "PutBackupVaultLockConfiguration", "DeleteBackupVaultLockConfiguration", "UpdateRecoveryPointLifecycle", "UpdateGlobalSettings", "UpdateRegionSettings"]},
    {"eventSource": ["backup.amazonaws.com"], "eventName": ["DeleteRecoveryPoint"],
     "requestParameters": {"backupVaultName": ["ejc3-backup", "ejc3-backup-dr", "fcvm-backups", "ejc3-backup-recovery"]}},
    {"eventSource": ["ec2.amazonaws.com"],
     "eventName": ["AuthorizeSecurityGroupIngress", "ModifySecurityGroupRules"]},
]
FINDING_BRANCHES = [
    {"source": ["aws.guardduty"], "detail-type": ["GuardDuty Finding"],
     "detail": {"severity": [{"numeric": [">=", 4]}], "service": {"archived": [False]}}},
    {"source": ["aws.access-analyzer"], "detail-type": ["Access Analyzer Finding"],
     "detail": {"status": ["ACTIVE"]}},
    {"source": ["aws.securityhub"], "detail-type": ["Security Hub Findings - Imported"],
     "detail": {"findings": {"Severity": {"Label": ["HIGH", "CRITICAL"]},
                             "ProductArn": [{"anything-but": {"suffix": [":product/aws/inspector", ":product/aws/guardduty"]}}],
                             "RecordState": ["ACTIVE"], "Workflow": {"Status": ["NEW"]}}}},
    {"source": ["aws.inspector2"], "detail-type": ["Inspector2 Finding"],
     "detail": {"severity": ["HIGH", "CRITICAL"], "status": ["ACTIVE"]}},
]
ROOT_BRANCH = {"detail-type": ["AWS API Call via CloudTrail", "AWS Console Sign In via CloudTrail"],
               "detail": {"userIdentity": {"type": ["Root"]}}}
LOGIN_BRANCH = {"detail-type": ["AWS Console Sign In via CloudTrail"],
                "detail": {"eventName": ["ConsoleLogin"], "responseElements": {"ConsoleLogin": ["Failure"]}}}
EXPECTED_PATTERNS = {
    REGIONAL_RULES[0]: {"$or": FINDING_BRANCHES},
    REGIONAL_RULES[1]: {"$or": [ROOT_BRANCH, LOGIN_BRANCH,
        {"detail-type": ["AWS API Call via CloudTrail"], "detail": {"$or": API_DETAILS}}]},
}
PATTERNS = {name: json.dumps(pattern, separators=(",", ":")) for name, pattern in EXPECTED_PATTERNS.items()}
PATTERNS["security-alerts-notify"] = json.dumps({"account": [MAIN, STAGING]})


def canonical(value):
    """Ignore JSON object ordering, never collapse false/0 or true/1."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def matches(pattern, event):
    """Bounded fixture matcher for only the operators in the reviewed patterns.

    This is not an EventBridge implementation; unsupported operators fail loudly.
    Native HCL proves the actual deployed expression matches these fixtures.
    """
    if isinstance(event, list):
        return any(matches(pattern, member) for member in event)
    if isinstance(pattern, dict):
        if set(pattern) == {"anything-but"}:
            excluded = pattern["anything-but"]
            if not isinstance(excluded, dict) or set(excluded) != {"suffix"} or not isinstance(excluded["suffix"], list):
                raise AssertionError("Unreviewed anything-but operator")
            return isinstance(event, str) and not event.endswith(tuple(excluded["suffix"]))
        if set(pattern) == {"numeric"}:
            operator, threshold = pattern["numeric"]
            if operator != ">=":
                raise AssertionError("Unreviewed numeric operator")
            return type(event) in (int, float) and event >= threshold
        if not isinstance(event, dict):
            return False
        return all(any(matches(branch, event) for branch in value) if key == "$or"
                   else key in event and matches(value, event[key]) for key, value in pattern.items())
    if isinstance(pattern, list):
        return any(matches(option, event) for option in pattern)
    return type(pattern) is type(event) and pattern == event


class Config:
    def __init__(self, **kwargs):
        self.values = kwargs


class CloudWatch:
    def __init__(self, factory, account, region):
        self.factory, self.account, self.region = factory, account, region

    def get_metric_data(self, **kwargs):
        self.factory.reads.append((self.account, self.region, kwargs))
        response = {"MetricDataResults": [
            {"Id": query["Id"], "StatusCode": "Complete", "Values": [], "Timestamps": []}
            for query in kwargs["MetricDataQueries"]
        ]}
        hook = self.factory.metrics.get((self.account, self.region))
        return hook(response) if hook else response

    def put_metric_data(self, **kwargs):
        if self.factory.publish_error:
            raise RuntimeError("synthetic private diagnostic never logged")
        self.factory.published.append(kwargs)
        return {}


class SQS:
    def __init__(self, factory, account, region):
        self.factory, self.account, self.region = factory, account, region

    def get_queue_attributes(self, **kwargs):
        self.factory.queue_reads.append((self.account, self.region, kwargs))
        queue = kwargs["QueueUrl"].rsplit("/", 1)[1]
        result = self.factory.queues.get((self.account, self.region, queue), {key: "0" for key in COUNTERS})
        if isinstance(result, Exception):
            raise result
        return {"Attributes": copy.deepcopy(result)}


class Events:
    def __init__(self, factory, account, region):
        self.factory, self.account, self.region = factory, account, region

    def describe_rule(self, **kwargs):
        self.factory.rule_reads.append((self.account, self.region, kwargs))
        name, bus = kwargs["Name"], kwargs.get("EventBusName")
        response = {"Name": name, "State": "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS" if name in ("security-forward-audit", "security-alerts-notify") else "ENABLED",
                    "Arn": "arn:aws:events:" + self.region + ":" + self.account + ":rule/" + (bus + "/" if bus else "") + name}
        if name == "security-delivery-health":
            response["ScheduleExpression"] = "rate(5 minutes)"
        else:
            response["EventPattern"] = PATTERNS[name] if name in PATTERNS else json.dumps({"account": [MAIN, STAGING]})
        override = self.factory.rules.get((self.account, self.region, name))
        if isinstance(override, Exception):
            raise override
        return override(copy.deepcopy(response)) if override else response

    def list_targets_by_rule(self, **kwargs):
        self.factory.target_reads.append((self.account, self.region, kwargs))
        assert kwargs["Limit"] == 2 and "NextToken" not in kwargs
        rule = kwargs["Rule"]
        if rule in REGIONAL_RULES:
            target = {"Id": "central-security-alerts", "Arn": "arn:aws:events:us-west-1:" + MAIN + ":event-bus/security-alerts",
                      "RoleArn": "arn:aws:iam::" + self.account + ":role/security-findings-forward",
                      "DeadLetterConfig": {"Arn": "arn:aws:sqs:" + self.region + ":" + self.account + ":security-findings-delivery-dlq"}}
        elif rule == "security-alerts-notify":
            target = {"Id": "confirmed-operator-email", "Arn": SNS_TOPIC,
                      "RoleArn": "arn:aws:iam::" + MAIN + ":role/security-alerts-notify",
                      "DeadLetterConfig": {"Arn": "arn:aws:sqs:us-west-1:" + MAIN + ":security-central-delivery-dlq"},
                      "RetryPolicy": {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185}}
        elif rule == "security-delivery-health":
            target = {"Id": rule, "Arn": "arn:aws:lambda:us-west-1:" + MAIN + ":function:security-delivery-health",
                      "DeadLetterConfig": {"Arn": "arn:aws:sqs:us-west-1:" + MAIN + ":security-central-delivery-dlq"},
                      "RetryPolicy": {"MaximumEventAgeInSeconds": 300, "MaximumRetryAttempts": 2}}
        else:
            raise AssertionError("Unexpected rule " + rule)
        response = {"Targets": [target]}
        override = self.factory.targets.get((self.account, self.region, rule))
        if isinstance(override, Exception):
            raise override
        return override(copy.deepcopy(response)) if override else response


class STS:
    def __init__(self, factory):
        self.factory = factory

    def assume_role(self, **kwargs):
        self.factory.assumed.append(kwargs)
        if self.factory.assume_error:
            raise RuntimeError("synthetic secret must not appear in logs")
        return {"Credentials": {"AccessKeyId": "synthetic-access", "SecretAccessKey": "synthetic-secret", "SessionToken": "synthetic-session"}}


class Session:
    def __init__(self, factory, account):
        self.factory, self.account = factory, account

    def client(self, name, region_name="us-west-1", config=None):
        # Boto3 Sessions must never be used concurrently from worker threads.
        assert threading.get_ident() == self.factory.main_thread
        assert config.values == {"connect_timeout": 2, "read_timeout": 3, "retries": {"total_max_attempts": 1}}
        if (self.account, region_name) in self.factory.client_errors:
            raise RuntimeError("synthetic client failure")
        if name == "cloudwatch":
            return CloudWatch(self.factory, self.account, region_name)
        if name == "sqs":
            return SQS(self.factory, self.account, region_name)
        if name == "events":
            return Events(self.factory, self.account, region_name)
        if name == "sts" and self.account == MAIN:
            return STS(self.factory)
        raise AssertionError("Unexpected AWS capability: " + name)


class Factory:
    def __init__(self):
        self.main_thread = threading.get_ident()
        self.reads, self.queue_reads, self.published, self.assumed = [], [], [], []
        self.metrics, self.queues, self.client_errors = {}, {}, set()
        self.rules, self.targets = {}, {}
        self.rule_reads, self.target_reads = [], []
        self.assume_error = self.publish_error = False

    def Session(self, **kwargs):
        if "aws_access_key_id" in kwargs:
            assert kwargs == {"aws_access_key_id": "synthetic-access", "aws_secret_access_key": "synthetic-secret", "aws_session_token": "synthetic-session"}
            return Session(self, STAGING)
        assert kwargs == {"region_name": "us-west-1"}
        return Session(self, MAIN)


def load(factory):
    config_module = types.ModuleType("botocore.config")
    config_module.Config = Config
    fake_modules = {"boto3": types.SimpleNamespace(Session=factory.Session),
                    "botocore": types.ModuleType("botocore"), "botocore.config": config_module}
    namespace = {}
    with patch.dict(sys.modules, fake_modules):
        exec(compile(SOURCE, "security-monitoring.tf:watchdog", "exec"), namespace)
    return namespace


def block(source, resource_type, name):
    start = source.index('resource "' + resource_type + '" "' + name + '" {')
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            return source[start:index + 1]
    raise AssertionError("Unclosed Terraform resource")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.module = load(self.factory)

    def run_handler(self, regions=None, extra=None):
        env = {"MAIN_ACCOUNT": MAIN, "STAGING_ACCOUNT": STAGING, "REGIONS": json.dumps(regions or REGIONS), "SNS_TOPIC_ARN": SNS_TOPIC,
               "EVENT_PATTERNS": json.dumps(PATTERNS)}
        env.update(extra or {})
        output = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(output):
            values = self.module["handler"]({}, None)
        return values, json.loads(output.getvalue())

    def inspect(self, account=MAIN, region="us-east-1", now=NOW):
        return self.module["inspect_region"](CloudWatch(self.factory, account, region),
            SQS(self.factory, account, region), Events(self.factory, account, region), account, region, MAIN, SNS_TOPIC, now,
            {name: canonical(json.loads(value)) for name, value in PATTERNS.items()})

    def test_complete_empty_failure_metrics_are_healthy(self):
        values, log = self.run_handler()
        self.assertEqual(values, {"FailedDeliveries": 0, "QueuedEvents": 0, "ObserverErrors": 0, "Heartbeat": 1})
        self.assertEqual(log, {"checked_regions": 34, "unhealthy": []})

    def test_exact_coverage_and_monthly_metric_request_budget(self):
        self.run_handler()
        self.assertEqual(len(REGIONS), 17)
        self.assertEqual({(account, region) for account, region, _ in self.factory.reads},
                         {(account, region) for account in (MAIN, STAGING) for region in REGIONS})
        requested = sum(len(call["MetricDataQueries"]) for _, _, call in self.factory.reads)
        self.assertEqual(requested, 72)
        self.assertEqual(requested * 288 * 30, 622080)
        self.assertEqual(len(self.factory.queue_reads), 35)
        self.assertEqual(len(self.factory.rule_reads), 70)
        self.assertEqual(len(self.factory.target_reads), 70)

    def test_disabled_missing_or_wrong_rule_is_not_a_healthy_quiet_region(self):
        for override in (lambda r: dict(r, State="DISABLED"), lambda r: {},
                         lambda r: dict(r, Name="other"), lambda r: dict(r, Arn="other"),
                         RuntimeError("ResourceNotFoundException"), TimeoutError("private diagnostic")):
            with self.subTest(override=override):
                self.factory.rules[(MAIN, "us-east-1", "security-forward-findings")] = override
                values, log = self.run_handler(["us-east-1"])
                self.assertGreaterEqual(values["ObserverErrors"], 1)
                self.assertEqual(values["FailedDeliveries"], 0)
                self.assertEqual(values["QueuedEvents"], 0)
                self.assertNotIn("private diagnostic", json.dumps(log))

    def test_audit_read_only_management_state_cannot_be_downgraded(self):
        for rule, invalid in (("security-forward-audit", "ENABLED"),
                              ("security-forward-audit", "DISABLED"),
                              ("security-forward-findings", "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS")):
            with self.subTest(rule=rule, state=invalid):
                self.factory.rules = {(MAIN, "us-east-1", rule): lambda response: dict(response, State=invalid)}
                result = self.inspect()
                self.assertGreaterEqual(result["errors"], 1)
                self.assertEqual(result["failures"], 0)
                self.assertEqual(result["depth"], 0)

    def test_central_notifications_require_read_only_management_state(self):
        # Forwarding CloudTrail onto a custom bus is not a documented exemption
        # from the read-only management-event state requirement on its rule.
        for state in ("ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS", "ENABLED", "DISABLED"):
            with self.subTest(state=state):
                self.factory.rules = {(MAIN, "us-west-1", "security-alerts-notify"):
                    lambda response: dict(response, State=state)}
                result = self.inspect(region="us-west-1")
                self.assertEqual(result["errors"] == 0, state == "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS")
                self.assertEqual(result["failures"], 0)
                self.assertEqual(result["depth"], 0)

    def test_removed_duplicate_paginated_or_denied_targets_fail_closed(self):
        for override in (lambda r: {"Targets": []}, lambda r: {},
                         lambda r: {"Targets": r["Targets"] * 2}, lambda r: dict(r, NextToken="do-not-follow"),
                         RuntimeError("AccessDenied"), TimeoutError("private diagnostic")):
            with self.subTest(override=override):
                self.factory.targets[(STAGING, "us-east-1", "security-forward-findings")] = override
                values, _ = self.run_handler(["us-east-1"])
                self.assertGreaterEqual(values["ObserverErrors"], 1)

    def test_target_destination_role_dlq_retries_and_payload_are_exact(self):
        for rule in (*REGIONAL_RULES, "security-alerts-notify", "security-delivery-health"):
            for key, value in (("Arn", "arn:foreign"), ("RoleArn", "arn:foreign"), ("Id", "other"),
                               ("DeadLetterConfig", {}), ("RetryPolicy", {}), ("Input", "{}"),
                               ("InputTransformer", {})):
                def change(response):
                    response["Targets"][0][key] = value
                    return response
                with self.subTest(rule=rule, key=key):
                    self.factory.targets = {(MAIN, "us-west-1", rule): change}
                    self.assertGreaterEqual(self.inspect(region="us-west-1")["errors"], 1)

    def test_event_bus_retry_policy_is_absent_not_ignored(self):
        # EventBridge rejects RetryPolicy for event-bus targets. Absence is the
        # exact supported contract, not permission to ignore added target fields.
        for account in (MAIN, STAGING):
            for rule in REGIONAL_RULES:
                with self.subTest(account=account, rule=rule, policy="absent"):
                    self.factory.targets = {}
                    self.assertEqual(self.inspect(account=account)["errors"], 0)
                for policy in (None, {}, {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185}):
                    def change(response):
                        response["Targets"][0]["RetryPolicy"] = policy
                        return response
                    with self.subTest(account=account, rule=rule, policy=policy):
                        self.factory.targets = {(account, "us-east-1", rule): change}
                        self.assertGreaterEqual(self.inspect(account=account)["errors"], 1)

    def test_non_bus_retry_policies_remain_required_and_exact(self):
        for rule, expected in (("security-alerts-notify", {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185}),
                               ("security-delivery-health", {"MaximumEventAgeInSeconds": 300, "MaximumRetryAttempts": 2})):
            target = self.module["expected_target"](rule, MAIN, "us-west-1", MAIN, SNS_TOPIC)
            self.assertEqual(target["RetryPolicy"], expected)
            for policy in (None, dict(expected, MaximumEventAgeInSeconds=60), dict(expected, MaximumRetryAttempts=0)):
                def change(response):
                    if policy is None:
                        response["Targets"][0].pop("RetryPolicy")
                    else:
                        response["Targets"][0]["RetryPolicy"] = policy
                    return response
                with self.subTest(rule=rule, policy=policy):
                    self.factory.targets = {(MAIN, "us-west-1", rule): change}
                    self.assertGreaterEqual(self.inspect(region="us-west-1")["errors"], 1)

    def test_central_rules_and_watchdog_schedule_are_independently_checked(self):
        for rule in ("security-alerts-notify", "security-delivery-health"):
            with self.subTest(rule=rule):
                self.factory.rules = {(MAIN, "us-west-1", rule): lambda r: dict(r, State="DISABLED")}
                self.assertGreaterEqual(self.inspect(region="us-west-1")["errors"], 1)
        self.factory.rules = {(MAIN, "us-west-1", "security-delivery-health"): lambda r: dict(r, ScheduleExpression="rate(1 day)")}
        self.assertGreaterEqual(self.inspect(region="us-west-1")["errors"], 1)
        central = [request for _, _, request in self.factory.rule_reads if request["Name"] == "security-alerts-notify"]
        self.assertTrue(all(request["EventBusName"] == "security-alerts" for request in central))

    def test_event_api_failure_does_not_skip_other_read_only_health_checks(self):
        self.factory.rules[(MAIN, "us-east-1", "security-forward-findings")] = TimeoutError("unavailable")
        result = self.inspect()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(self.factory.target_reads), 2)
        self.assertEqual(len(self.factory.reads), 1)
        self.assertEqual(len(self.factory.queue_reads), 1)

    def test_metric_names_case_dimensions_and_query_window(self):
        self.inspect(region="us-west-1")
        request = self.factory.reads[0][2]
        queries = request["MetricDataQueries"]
        self.assertEqual(request["StartTime"], NOW - datetime.timedelta(minutes=15))
        self.assertEqual(request["EndTime"], NOW)
        self.assertEqual(request["MaxDatapoints"], 100)
        self.assertEqual(len(queries), 6)
        for index, query in enumerate(queries):
            metric = query["MetricStat"]["Metric"]
            self.assertEqual(metric["MetricName"], ["FailedInvocations", "InvocationsFailedToBeSentToDlq"][index % 2])
            self.assertEqual(query["MetricStat"]["Stat"], "Sum")
            self.assertEqual(query["MetricStat"]["Period"], 300)
            dimensions = {item["Name"]: item["Value"] for item in metric["Dimensions"]}
            self.assertEqual(dimensions.get("EventBusName"), "security-alerts" if index in (2, 3) else None)

    def test_staging_west1_does_not_query_main_only_resources(self):
        self.inspect(account=STAGING, region="us-west-1")
        self.assertEqual(len(self.factory.reads[0][2]["MetricDataQueries"]), 2)
        self.assertEqual(len(self.factory.queue_reads), 1)

    def test_both_epoch_parities_keep_full_topology_and_exact_metric_budget(self):
        original_datetime = datetime.datetime
        for offset in (0, 5):
            instant = NOW + datetime.timedelta(minutes=offset)
            class Clock(original_datetime):
                @classmethod
                def now(cls, tz=None):
                    return instant
            with self.subTest(offset=offset):
                self.factory = Factory()
                self.module = load(self.factory)
                with patch.dict(self.module, datetime=types.SimpleNamespace(datetime=Clock, timedelta=datetime.timedelta, timezone=datetime.timezone)):
                    values, log = self.run_handler()
                self.assertEqual(values["ObserverErrors"], 0)
                self.assertEqual(log["checked_regions"], 34)
                self.assertEqual(len(self.factory.rule_reads), 70)
                self.assertEqual(len(self.factory.target_reads), 70)
                self.assertEqual(len(self.factory.queue_reads), 35)
                self.assertEqual(sum(len(call["MetricDataQueries"]) for _, _, call in self.factory.reads), 72)
                expected_sample = REGIONAL_RULES[int(instant.timestamp()) // 300 % 2]
                for account, region, call in self.factory.reads:
                    queried = [dict((d["Name"], d["Value"]) for d in q["MetricStat"]["Metric"]["Dimensions"])["RuleName"]
                               for q in call["MetricDataQueries"]]
                    self.assertEqual(queried.count(expected_sample), 2)
                    self.assertNotIn(REGIONAL_RULES[1 - REGIONAL_RULES.index(expected_sample)], queried)
                    self.assertEqual(len(queried), 6 if account == MAIN and region == "us-west-1" else 2)
                    self.assertEqual(call["StartTime"], instant - datetime.timedelta(minutes=15))
                    self.assertEqual(call["EndTime"], instant)

    def test_alternating_samples_revisit_both_rules_with_five_minute_overlap(self):
        sampled = []
        for offset in (0, 5, 10):
            self.inspect(now=NOW + datetime.timedelta(minutes=offset))
            request = self.factory.reads[-1][2]
            sampled.append(request["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"])
        self.assertEqual(set(sampled[:2]), set(REGIONAL_RULES))
        self.assertEqual(sampled[0], sampled[2])
        first, third = self.factory.reads[0][2], self.factory.reads[2][2]
        self.assertEqual(first["EndTime"] - third["StartTime"], datetime.timedelta(minutes=5))

    def test_pattern_object_order_and_whitespace_are_healthy(self):
        for rule in PATTERNS:
            pattern = json.loads(PATTERNS[rule])
            # Reverse object keys recursively, keeping numeric operator arrays in order.
            def reorder(value):
                if isinstance(value, dict):
                    return {key: reorder(value[key]) for key in reversed(value)}
                return [reorder(item) for item in value] if isinstance(value, list) else value
            with self.subTest(rule=rule):
                self.factory.rules = {(MAIN, "us-west-1", rule): lambda response: dict(response, EventPattern=json.dumps(reorder(pattern), indent=2))}
                self.assertEqual(self.inspect(region="us-west-1")["errors"], 0)

    def test_missing_malformed_empty_and_changed_patterns_fail_closed(self):
        for rule in PATTERNS:
            for invalid in (None, "", "not-json", "{}", "[]", "null", '"text"',
                            '{"source":["aws.foreign"]}', '{"account":["999999999999"]}',
                            '{"source":["aws.a"],"source":["aws.b"]}', '{"severity":[NaN]}'):
                with self.subTest(rule=rule, invalid=invalid):
                    self.factory.rules = {(MAIN, "us-west-1", rule): lambda response: dict(response, EventPattern=invalid)}
                    result = self.inspect(region="us-west-1")
                    self.assertGreaterEqual(result["errors"], 1)
                    self.assertEqual(result["failures"], 0)
                    self.assertEqual(result["depth"], 0)

    def test_semantic_pattern_drift_detects_narrowing_broadening_and_scalar_types(self):
        cases = []
        for rule in REGIONAL_RULES:
            removed = copy.deepcopy(EXPECTED_PATTERNS[rule])
            removed["$or"].pop()
            added = copy.deepcopy(EXPECTED_PATTERNS[rule])
            added["$or"].append({"source": ["aws.unreviewed"]})
            cases.extend([(rule, removed), (rule, added)])
        archived_number = copy.deepcopy(EXPECTED_PATTERNS[REGIONAL_RULES[0]])
        archived_number["$or"][0]["detail"]["service"]["archived"] = [0]
        numeric_reordered = copy.deepcopy(EXPECTED_PATTERNS[REGIONAL_RULES[0]])
        numeric_reordered["$or"][0]["detail"]["severity"][0]["numeric"] = [4, ">="]
        cases.extend([(REGIONAL_RULES[0], archived_number), (REGIONAL_RULES[0], numeric_reordered),
                      ("security-alerts-notify", {"account": [MAIN]}),
                      ("security-alerts-notify", {"account": [MAIN, STAGING, "999999999999"]})])
        for rule, pattern in cases:
            with self.subTest(rule=rule, pattern=pattern):
                self.factory.rules = {(MAIN, "us-west-1", rule): lambda response: dict(response, EventPattern=json.dumps(pattern))}
                self.assertGreaterEqual(self.inspect(region="us-west-1")["errors"], 1)

    def test_rule_pattern_and_schedule_modes_cannot_silently_expand(self):
        for rule in PATTERNS:
            with self.subTest(rule=rule):
                self.factory.rules = {(MAIN, "us-west-1", rule): lambda response: dict(response, ScheduleExpression="rate(1 minute)")}
                self.assertGreaterEqual(self.inspect(region="us-west-1")["errors"], 1)
        self.factory.rules = {(MAIN, "us-west-1", "security-delivery-health"): lambda response: dict(response, EventPattern='{"source":["aws.unreviewed"]}')}
        self.assertGreaterEqual(self.inspect(region="us-west-1")["errors"], 1)

    def test_invalid_expected_pattern_configuration_fails_before_aws_calls(self):
        variants = [{}, [], None, {name: value for name, value in PATTERNS.items() if name != REGIONAL_RULES[1]},
                    dict(PATTERNS, foreign='{"source":["aws.unreviewed"]}'),
                    dict(PATTERNS, **{REGIONAL_RULES[0]: "{}"})]
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, TypeError)):
                self.run_handler(extra={"EVENT_PATTERNS": json.dumps(invalid)})
        self.assertEqual(self.factory.assumed, [])
        self.assertEqual(self.factory.reads, [])

    def test_failure_and_all_queue_states_raise_health_gauges(self):
        def failures(response):
            response["MetricDataResults"][0]["Values"] = [1, 2]
            response["MetricDataResults"][1]["Values"] = [1]
            return response
        self.factory.metrics[(MAIN, "us-east-1")] = failures
        self.factory.queues[(STAGING, "us-east-1", "security-findings-delivery-dlq")] = dict(zip(COUNTERS, ["2", "3", "4"]))
        values, log = self.run_handler(["us-east-1"])
        self.assertEqual(values["FailedDeliveries"], 4)
        self.assertEqual(values["QueuedEvents"], 9)
        self.assertEqual(values["ObserverErrors"], 0)
        self.assertEqual(len(log["unhealthy"]), 2)

    def test_partial_paginated_missing_duplicate_unknown_and_messages_are_errors(self):
        def partial(response):
            response["MetricDataResults"][0]["StatusCode"] = "PartialData"
            return response
        def missing(response):
            response["MetricDataResults"].pop()
            return response
        def duplicate(response):
            response["MetricDataResults"][1]["Id"] = "m0"
            return response
        def unknown(response):
            response["MetricDataResults"][1]["Id"] = "foreign"
            return response
        for change in (partial, missing, duplicate, unknown,
                       lambda response: dict(response, NextToken="bounded-do-not-follow"),
                       lambda response: dict(response, Messages=[{"Code": "InternalError"}])):
            with self.subTest(change=change):
                self.factory.metrics[(MAIN, "us-east-1")] = change
                self.assertGreaterEqual(self.inspect()["errors"], 1)

    def test_invalid_metric_values_never_publish_nan_or_negative_success(self):
        for invalid in ([-1], [float("nan")], [float("inf")], ["1"], [True], {}):
            def malformed(response):
                response["MetricDataResults"][0]["Values"] = invalid
                return response
            with self.subTest(invalid=invalid):
                self.factory.metrics[(MAIN, "us-east-1")] = malformed
                result = self.inspect()
                self.assertGreaterEqual(result["errors"], 1)
                self.assertEqual(result["failures"], 0)

    def test_queue_missing_malformed_or_denied_is_not_healthy_zero(self):
        for invalid in ({}, {key: "bad" for key in COUNTERS}, {key: "-1" for key in COUNTERS}, RuntimeError("denied")):
            with self.subTest(invalid=invalid):
                self.factory.queues[(MAIN, "us-east-1", "security-findings-delivery-dlq")] = invalid
                self.assertGreaterEqual(self.inspect()["errors"], 1)

    def test_cloudwatch_timeout_does_not_skip_dlq_count(self):
        def timeout(response):
            raise TimeoutError("synthetic secret-bearing diagnostic")
        self.factory.metrics[(MAIN, "us-east-1")] = timeout
        result = self.inspect()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(len(self.factory.queue_reads), 1)

    def test_assume_failure_checks_main_and_reports_partial_coverage(self):
        self.factory.assume_error = True
        values, log = self.run_handler()
        self.assertEqual(log["checked_regions"], 17)
        self.assertEqual(values["ObserverErrors"], 1)
        self.assertEqual(values["Heartbeat"], 1)
        self.assertNotIn("synthetic", json.dumps(log))

    def test_exact_read_role_short_session_and_no_credential_log(self):
        _, log = self.run_handler(["us-east-1"])
        self.assertEqual(self.factory.assumed, [{"RoleArn": "arn:aws:iam::" + STAGING + ":role/security-delivery-observer",
                         "RoleSessionName": "security-delivery-health", "DurationSeconds": 900}])
        self.assertNotIn("synthetic", json.dumps(log))

    def test_client_and_worker_exceptions_are_observer_errors(self):
        self.factory.client_errors.add((STAGING, "us-east-1"))
        values, log = self.run_handler(["us-east-1"])
        self.assertEqual(values["ObserverErrors"], 1)
        self.assertEqual(log["checked_regions"], 1)
        with patch.dict(self.module, inspect_region=lambda *args: (_ for _ in ()).throw(RuntimeError("failure"))):
            values, log = self.run_handler(["us-east-1"])
        self.assertEqual(values["ObserverErrors"], 2)
        self.assertEqual(log["checked_regions"], 0)

    def test_only_scoped_counts_are_read_and_four_metrics_are_published(self):
        self.run_handler(["us-west-1"])
        for account, region, request in self.factory.queue_reads:
            self.assertTrue(request["QueueUrl"].startswith("https://sqs." + region + ".amazonaws.com/" + account + "/security-"))
            self.assertEqual(request["AttributeNames"], COUNTERS)
        self.assertEqual(len(self.factory.published), 1)
        publish = self.factory.published[0]
        self.assertEqual(publish["Namespace"], "SecurityDelivery")
        self.assertEqual(len(publish["MetricData"]), 4)
        self.assertTrue(all(metric["Unit"] == "Count" for metric in publish["MetricData"]))

    def test_failed_publish_never_reports_a_successful_heartbeat(self):
        self.factory.publish_error = True
        with self.assertRaises(RuntimeError):
            self.run_handler(["us-east-1"])
        self.assertEqual(self.factory.published, [])

    def test_bounded_region_account_validation_happens_before_aws_calls(self):
        for invalid in ([], ["us-east-1"] * 18, ["us-east-1", "us-east-1"], [None], [{}], ["../host"], "us-east-1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.run_handler(extra={"REGIONS": json.dumps(invalid)})
        for invalid in ({"MAIN_ACCOUNT": "invalid"}, {"STAGING_ACCOUNT": MAIN},
                        {"SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:" + MAIN + ":wrong-region"},
                        {"SNS_TOPIC_ARN": "arn:aws:sns:us-west-1:" + STAGING + ":wrong-account"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.run_handler(extra=invalid)
        self.assertEqual(self.factory.assumed, [])
        self.assertEqual(self.factory.reads, [])

    def test_worker_concurrency_is_capped_at_six(self):
        original = concurrent.futures.ThreadPoolExecutor
        with patch.object(concurrent.futures, "ThreadPoolExecutor", wraps=original) as executor:
            self.run_handler()
        executor.assert_called_once_with(max_workers=6)


class TerraformSafetyTests(unittest.TestCase):
    @staticmethod
    def event_pattern_fixtures():
        fixtures = []
        for severity in (3.9, 4, 8):
            for archived in (False, True):
                fixtures.append(({"source": "aws.guardduty", "detail-type": "GuardDuty Finding",
                    "detail": {"severity": severity, "service": {"archived": archived}}},
                    REGIONAL_RULES[0] if severity >= 4 and not archived else None))
        for status in ("ACTIVE", "ARCHIVED", "RESOLVED"):
            fixtures.append(({"source": "aws.access-analyzer", "detail-type": "Access Analyzer Finding",
                              "detail": {"status": status}}, REGIONAL_RULES[0] if status == "ACTIVE" else None))
        for severity in ("HIGH", "CRITICAL", "MEDIUM"):
            for state in ("ACTIVE", "ARCHIVED"):
                for workflow in ("NEW", "NOTIFIED", "RESOLVED"):
                    fixtures.append(({"source": "aws.securityhub", "detail-type": "Security Hub Findings - Imported",
                        "detail": {"findings": [{"Severity": {"Label": severity}, "RecordState": state,
                                                "ProductArn": "arn:aws:securityhub:us-west-1::product/aws/securityhub",
                                                "Workflow": {"Status": workflow}}]}},
                        REGIONAL_RULES[0] if severity in ("HIGH", "CRITICAL") and state == "ACTIVE" and workflow == "NEW" else None))
            for status in ("ACTIVE", "SUPPRESSED", "CLOSED"):
                fixtures.append(({"source": "aws.inspector2", "detail-type": "Inspector2 Finding",
                                  "detail": {"severity": severity, "status": status}},
                                  REGIONAL_RULES[0] if severity in ("HIGH", "CRITICAL") and status == "ACTIVE" else None))
        # Inspector and GuardDuty keep one native notification path, including when
        # Security Hub imports the same finding. Other product paths still notify.
        for region in REGIONS:
            for product in ("aws/inspector", "aws/guardduty", "aws/securityhub", "partner/inspector"):
                fixtures.append(({"source": "aws.securityhub", "detail-type": "Security Hub Findings - Imported",
                    "detail": {"findings": [{"Severity": {"Label": "HIGH"}, "RecordState": "ACTIVE",
                        "ProductArn": f"arn:aws:securityhub:{region}::product/{product}",
                        "Workflow": {"Status": "NEW"}}]}},
                    None if product in ("aws/inspector", "aws/guardduty") else REGIONAL_RULES[0]))
        # A finding with the right detail body but a different source must not page.
        for event, expected in list(fixtures):
            if expected:
                wrong_source = copy.deepcopy(event)
                wrong_source["source"] = "aws.unreviewed"
                fixtures.append((wrong_source, None))
        for kind in ("AWS API Call via CloudTrail", "AWS Console Sign In via CloudTrail", "Other Event"):
            for identity in ("Root", "IAMUser"):
                fixtures.append(({"detail-type": kind, "detail": {"userIdentity": {"type": identity}, "eventName": "ListBuckets"}},
                    REGIONAL_RULES[1] if identity == "Root" and kind != "Other Event" else None))
        for outcome in ("Success", "Failure"):
            for kind in ("AWS Console Sign In via CloudTrail", "AWS API Call via CloudTrail"):
                fixtures.append(({"detail-type": kind, "detail": {"eventName": "ConsoleLogin", "responseElements": {"ConsoleLogin": outcome}}},
                    REGIONAL_RULES[1] if outcome == "Failure" and kind == "AWS Console Sign In via CloudTrail" else None))
        for details in API_DETAILS:
            for source in details["eventSource"]:
                for action in details["eventName"]:
                    vaults = details.get("requestParameters", {}).get("backupVaultName", [None])
                    for vault in vaults:
                        event = {"detail-type": "AWS API Call via CloudTrail",
                                 "detail": {"eventSource": source, "eventName": action}}
                        if vault is not None:
                            event["detail"]["requestParameters"] = {"backupVaultName": vault}
                        fixtures.append((event, REGIONAL_RULES[1]))
                        # A root tampering attempt matches multiple original OR
                        # branches, but the split must still deliver it only once.
                        root_event = copy.deepcopy(event)
                        root_event["detail"]["userIdentity"] = {"type": "Root"}
                        root_event["detail"]["errorCode"] = "AccessDenied"
                        fixtures.append((root_event, REGIONAL_RULES[1]))
                        foreign = copy.deepcopy(event)
                        foreign["detail"]["eventSource"] = "unreviewed.amazonaws.com"
                        fixtures.append((foreign, None))
            fixtures.append(({"detail-type": "AWS API Call via CloudTrail", "detail": {
                "eventSource": details["eventSource"][0], "eventName": "OrdinaryReadOnlyCall"}}, None))
        for vault in ("processing", "checkpoint", "unrelated", None):
            fixtures.append(({"detail-type": "AWS API Call via CloudTrail", "detail": {
                "eventSource": "backup.amazonaws.com", "eventName": "DeleteRecoveryPoint",
                "requestParameters": {"backupVaultName": vault}}}, None))
        return fixtures

    def test_split_pattern_semantics_preserve_all_branches_without_duplicate_delivery(self):
        fixtures = self.event_pattern_fixtures()
        original = {"$or": FINDING_BRANCHES + [ROOT_BRANCH, LOGIN_BRANCH] + [
            {"detail-type": ["AWS API Call via CloudTrail"], "detail": details} for details in API_DETAILS]}
        self.assertGreater(len(fixtures), 250)
        for event, expected in fixtures:
            with self.subTest(event=event):
                self.assertEqual(matches(original, event), expected is not None)
                self.assertEqual([name for name, pattern in EXPECTED_PATTERNS.items() if matches(pattern, event)],
                                 [expected] if expected else [])

    def test_shipped_pattern_map_is_the_single_source_for_rules_and_watchdog(self):
        for name in ("findings",):
            rule = block(REGIONAL, "aws_cloudwatch_event_rule", name)
            self.assertRegex(rule, r"for_each\s*=\s*local\.security_event_patterns")
            self.assertRegex(rule, r"event_pattern\s*=\s*each\.value")
            self.assertRegex(rule, r"length\(each\.value\)\s*<=\s*2048")
            self.assertRegex(rule, r'state\s*=\s*each\.key\s*==\s*"security-forward-audit"\s*\?\s*"ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"\s*:\s*"ENABLED"')
            self.assertNotIn("is_enabled", rule)
        target = block(REGIONAL, "aws_cloudwatch_event_target", "central")
        self.assertRegex(target, r"for_each\s*=\s*local\.security_event_patterns")
        self.assertIn("aws_cloudwatch_event_rule.findings[each.key].name", target)
        self.assertRegex(REGIONAL, r'output "event_patterns"\s*\{\s*value\s*=\s*local\.security_event_patterns')
        function = block(TERRAFORM, "aws_lambda_function", "security_delivery_health")
        self.assertIn("module.security_main_us_west_1.event_patterns", function)
        self.assertIn("aws_cloudwatch_event_rule.security_notify.event_pattern", function)
        for name in ("security_delivery_health", "security_delivery_observer_staging"):
            policy = block(TERRAFORM, "aws_iam_role_policy", name)
            self.assertIn('"security-forward-findings", "security-forward-audit"', policy)
            self.assertNotIn(":rule/security-forward-*", policy)

    def test_central_rule_accepts_forwarded_management_reads_without_broadening_pattern(self):
        rule = block(TERRAFORM, "aws_cloudwatch_event_rule", "security_notify")
        self.assertRegex(rule, r'\bstate\s*=\s*"ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"')
        self.assertRegex(rule, r'event_pattern\s*=\s*jsonencode\(\{ account = local\.security_account_ids \}\)')
        self.assertNotIn("is_enabled", rule)

    @unittest.skipUnless(os.environ.get("SECURITY_TEST_TERRAFORM"), "optional native HCL fixture check; SDK-free semantic fixtures run above")
    def test_actual_shipped_event_patterns_match_fixtures_and_default_size_quota(self):
        start = re.search(r"security_event_patterns\s*=\s*\{", REGIONAL).end() - 1
        depth = 0
        for end in range(start, len(REGIONAL)):
            depth += (REGIONAL[end] == "{") - (REGIONAL[end] == "}")
            if not depth:
                break
        expression = REGIONAL[start:end + 1]
        expression = re.sub(r"(?m)^\s*#.*$", "", expression)
        expression = re.sub(r'(?<=[\w"\]})])\n(?=\s*["\w][^\n]*=)', ', ', expression)
        with tempfile.TemporaryDirectory(prefix="security-event-patterns-") as directory:
            result = subprocess.run([os.environ["SECURITY_TEST_TERRAFORM"], "console", "-no-color"],
                input="jsonencode(" + " ".join(expression.splitlines()) + ")\n", text=True,
                capture_output=True, timeout=15, cwd=directory,
                env={"CHECKPOINT_DISABLE": "1", "TF_CLI_CONFIG_FILE": "/dev/null", "TF_IN_AUTOMATION": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(json.loads(result.stdout.strip()))
        self.assertEqual(set(actual), set(REGIONAL_RULES))
        for name, encoded in actual.items():
            with self.subTest(rule=name):
                self.assertGreater(len(encoded), 0)
                self.assertLessEqual(len(encoded.encode("utf-8")), 2048)
                self.assertEqual(canonical(json.loads(encoded)), canonical(EXPECTED_PATTERNS[name]))
        # Evaluate the actual per-rule state expression, not a duplicated Python
        # decision: ENABLED alone silently omits root Get/List/Describe events.
        rule = block(REGIONAL, "aws_cloudwatch_event_rule", "findings")
        state = re.search(r'^\s*state\s*=\s*(.+)$', rule, re.M).group(1).replace("each.key", "name")
        with tempfile.TemporaryDirectory(prefix="security-event-rule-state-") as directory:
            result = subprocess.run([os.environ["SECURITY_TEST_TERRAFORM"], "console", "-no-color"],
                input='jsonencode({for name in ' + json.dumps(list(REGIONAL_RULES)) + ' : name => ' + state + '})\n',
                text=True, capture_output=True, timeout=15, cwd=directory,
                env={"CHECKPOINT_DISABLE": "1", "TF_CLI_CONFIG_FILE": "/dev/null", "TF_IN_AUTOMATION": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(json.loads(result.stdout.strip())), {
            "security-forward-findings": "ENABLED", "security-forward-audit": "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"})
        central = block(TERRAFORM, "aws_cloudwatch_event_rule", "security_notify")
        central_state = re.search(r'^\s*state\s*=\s*(.+)$', central, re.M)
        self.assertIsNotNone(central_state)
        self.assertEqual(self.native_json_value(central_state.group(1)), "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS")

    def test_posture_covers_both_source_regions_and_final_recovery_region(self):
        expected = {"security_main_us_west_1", "security_main_us_west_2",
                    "security_main_us_east_1", "security_staging_us_west_1",
                    "security_staging_us_east_1"}
        enabled = set()
        for name, body in re.findall(r'^module "([^"]+)" \{(.*?)^\}', TERRAFORM, re.M | re.S):
            if re.search(r'\bposture_enabled\s*=\s*local\.security_posture_enabled\b', body):
                enabled.add(name)
        self.assertEqual(enabled, expected)

    def test_approved_posture_uses_single_gate_without_disabling_foundation(self):
        self.assertRegex(TERRAFORM, r'\bsecurity_posture_enabled\s*=\s*true\b')
        self.assertNotRegex(TERRAFORM, r'\bposture_enabled\s*=\s*true\b')
        self.assertNotIn("posture_enabled", block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty"))

    def test_posture_keeps_reviewed_scanning_and_recording_scope(self):
        inspector = block(REGIONAL, "aws_inspector2_enabler", "security")
        self.assertRegex(inspector, r'resource_types\s*=\s*\["EC2",\s*"LAMBDA"\]')
        recorder = block(REGIONAL, "aws_config_configuration_recorder", "security")
        self.assertRegex(recorder, r'include_global_resource_types\s*=\s*var\.record_global_iam')
        self.assertRegex(recorder, r'recording_frequency\s*=\s*"DAILY"')
        self.assertRegex(recorder, r'resource_types\s*=\s*\["AWS::EC2::Instance",\s*"AWS::EC2::NetworkInterface",\s*"AWS::EC2::Volume"\]')
        hub = block(REGIONAL, "aws_securityhub_account", "security")
        self.assertRegex(hub, r'enable_default_standards\s*=\s*false')
        standard = block(REGIONAL, "aws_securityhub_standards_subscription", "foundational")
        self.assertIn("standards/aws-foundational-security-best-practices/v/1.0.0", standard)

    def test_external_analyzers_have_a_single_separate_owner(self):
        external = (ROOT / "security-external-access.tf").read_text()
        self.assertNotIn('resource "aws_accessanalyzer_analyzer"', REGIONAL)
        self.assertNotIn('resource "aws_accessanalyzer_analyzer"', TERRAFORM)
        self.assertEqual(external.count('resource "aws_accessanalyzer_analyzer"'), 34)
        self.assertEqual(external.count('resource "aws_iam_service_linked_role"'), 2)
        self.assertNotIn("posture_enabled", external)

    def test_defaults_have_a_single_separate_owner(self):
        defaults = (ROOT / "modules/security-defaults/main.tf").read_text()
        for kind in ("aws_ebs_encryption_by_default", "aws_ebs_snapshot_block_public_access",
                     "aws_ec2_instance_metadata_defaults"):
            self.assertNotIn(kind, REGIONAL)
            self.assertNotIn("posture_enabled", block(defaults, kind, "security"))

    def test_monitoring_reuses_shared_provider_aliases(self):
        self.assertNotRegex(TERRAFORM, re.compile(r'^provider "aws"', re.M))
        east1 = re.search(r'^module "security_staging_us_east_1" \{(.*?)^\}', TERRAFORM, re.M | re.S).group(1)
        self.assertRegex(east1, r'providers\s*=\s*\{ aws = aws\.staging_dr \}')
        self.assertNotIn("aws.security_staging_us_east_1", TERRAFORM)

    def test_optional_runner_and_jumpbox_references_are_gated(self):
        flow = block(TERRAFORM, "aws_flow_log", "security_main")
        roles = block(TERRAFORM, "aws_iam_role_policy", "security_session_logs")
        self.assertRegex(flow, r'var\.enable_github_runner\s*\?\s*\{ runner = aws_vpc\.runner\[0\]\.id \}\s*:\s*\{\}')
        self.assertRegex(roles, r'var\.enable_github_runner\s*\?\s*\{\s*runner\s*= aws_iam_role\.runner\[0\]\.name\s*ami_builder\s*= aws_iam_role\.ami_builder\[0\]\.name\s*\}\s*:\s*\{\}')
        self.assertRegex(roles, r'local\.jumpbox_admin_iam_needed\s*\?\s*\{ jumpbox = aws_iam_role\.jumpbox_admin\[0\]\.name \}\s*:\s*\{\}')

    def test_builder_transcript_grant_has_exact_actions_and_resources(self):
        roles = block(TERRAFORM, "aws_iam_role_policy", "security_session_logs")
        self.assertRegex(roles, r'ami_builder\s*=\s*aws_iam_role\.ami_builder\[0\]\.name')
        policy = roles.split("policy = jsonencode(", 1)[1]
        self.assertEqual(set(re.findall(r'"(s3:[^"]+)"', policy)),
                         {"s3:GetBucketLocation", "s3:GetEncryptionConfiguration", "s3:PutObject"})
        self.assertEqual(re.findall(r'^\s*Resource\s*=\s*(.+)$', policy, re.M),
                         ['aws_s3_bucket.security_config.arn', '"${aws_s3_bucket.security_config.arn}/sessions/*"'])
        self.assertEqual(re.findall(r'Effect\s*=\s*"([^"]+)"', policy), ["Allow", "Allow"])

    @unittest.skipUnless(os.environ.get("SECURITY_TEST_TERRAFORM"), "optional native HCL fixture check; SDK-free scope guards run above")
    def test_actual_optional_maps_render_with_absent_counted_resources(self):
        flow = block(TERRAFORM, "aws_flow_log", "security_main").split("for_each = ", 1)[1].split("\n  vpc_id", 1)[0]
        roles = block(TERRAFORM, "aws_iam_role_policy", "security_session_logs").split("for_each = ", 1)[1].split("\n  name", 1)[0]
        for runners in (False, True):
            for jumpbox in (False, True):
                with self.subTest(runners=runners, jumpbox=jumpbox):
                    replacements = {
                        "var.enable_github_runner": json.dumps(runners),
                        "local.jumpbox_admin_iam_needed": json.dumps(jumpbox),
                        "local.vpc_id": json.dumps("vpc-dev"),
                        "aws_vpc.runner": json.dumps([{"id": "vpc-runner"}] if runners else []),
                        "aws_iam_role.runner": json.dumps([{"name": "runner-role"}] if runners else []),
                        "aws_iam_role.ami_builder": json.dumps([{"name": "builder-role"}] if runners else []),
                        "aws_iam_role.jumpbox_admin": json.dumps([{"name": "jumpbox-role"}] if jumpbox else []),
                        "aws_iam_role.dev_server.name": json.dumps("dev-role"),
                        "aws_iam_role.nextjs_dev.name": json.dumps("nextjs-role"),
                        "aws_iam_role.dev_ebs_only.name": json.dumps("working-role"),
                    }
                    expression = '{ flow = ' + flow + ', roles = ' + roles + ' }'
                    for old, new in replacements.items():
                        expression = expression.replace(old, new)
                    # Console reads one expression line; preserve HCL object
                    # attribute separators while flattening the real map body.
                    expression = re.sub(r'(?<=[\w"\]])\n(?=\s*\w+\s*=)', ', ', expression)
                    # Empty, provider/backend-free directory: no AWS credentials or calls.
                    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                           "TF_CLI_CONFIG_FILE": "/dev/null", "CHECKPOINT_DISABLE": "1"}
                    with tempfile.TemporaryDirectory(prefix="security-optional-hcl-") as directory:
                        result = subprocess.run([os.environ["SECURITY_TEST_TERRAFORM"], "console", "-no-color"],
                            input="jsonencode(" + re.sub(r"\s+", " ", expression) + ")\n",
                            text=True, capture_output=True, timeout=10, cwd=directory, env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rendered = json.loads(json.loads(result.stdout.strip()))
                    self.assertEqual(rendered["flow"], {"dev": "vpc-dev", **({"runner": "vpc-runner"} if runners else {})})
                    self.assertEqual(rendered["roles"], {"dev": "dev-role", "nextjs": "nextjs-role", "working": "working-role",
                        **({"runner": "runner-role", "ami_builder": "builder-role"} if runners else {}),
                        **({"jumpbox": "jumpbox-role"} if jumpbox else {})})

    def test_guardduty_all_current_optional_features_are_disabled_at_creation(self):
        features = re.search(r'guardduty_base_optional_features\s*=\s*\[([^]]+)\]', REGIONAL)
        self.assertIsNotNone(features)
        self.assertEqual(set(re.findall(r'"([A-Z0-9_]+)"', features.group(1))), GUARDDUTY_BASE_FEATURES)
        regions = re.search(r'guardduty_ai_analyst_regions\s*=\s*\[([^]]+)\]', REGIONAL)
        self.assertIsNotNone(regions)
        self.assertEqual(set(re.findall(r'"([a-z0-9-]+)"', regions.group(1))), GUARDDUTY_ANALYST_REGIONS)
        self.assertRegex(REGIONAL, re.compile(r'guardduty_optional_features\s*=\s*concat\(\s*local\.guardduty_base_optional_features,\s*'
            r'contains\(local\.guardduty_ai_analyst_regions, data\.aws_region\.current\.name\)\s*\?\s*\["AI_ANALYST"\]\s*:\s*\[\]', re.S))
        detector = block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty")
        self.assertIn('type_name = "AWS::GuardDuty::Detector"', detector)
        self.assertRegex(detector, r'Enable\s*=\s*true')
        self.assertIn('Name = name, Status = "DISABLED"', detector)
        self.assertIn('Name = agent, Status = "DISABLED"', detector)
        self.assertNotRegex(REGIONAL, re.compile(r'^resource "aws_guardduty_detector(?:_feature)?"', re.M))
        self.assertNotIn("DataSources", detector)
        self.assertIn('prevent_destroy = true', detector)
        self.assertIn('jsondecode(self.properties).Enable == true', detector)
        self.assertIn('name, "MISSING") == "DISABLED"', detector)
        self.assertIn('feature.Status == "DISABLED"', detector)

    def test_guardduty_region_matrix_covers_exactly_the_declared_regions(self):
        reviewed = re.search(r'guardduty_reviewed_regions\s*=\s*\[([^]]+)\]', REGIONAL)
        self.assertIsNotNone(reviewed)
        self.assertEqual(set(re.findall(r'"([a-z0-9-]+)"', reviewed.group(1))), set(REGIONS))
        self.assertEqual(len(REGIONS), 17)
        self.assertEqual(len(GUARDDUTY_ANALYST_REGIONS), 10)
        self.assertTrue(GUARDDUTY_ANALYST_REGIONS.issubset(REGIONS))
        self.assertNotIn("us-west-1", GUARDDUTY_ANALYST_REGIONS)
        detector = block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty")
        self.assertIn("contains(local.guardduty_reviewed_regions, data.aws_region.current.name)", detector)

    @staticmethod
    def expanded_guardduty_expression(expression):
        """Inline only actual GuardDuty locals into an isolated console fixture."""
        for _ in range(20):
            reference = re.search(r'local\.(guardduty_[a-z_]+)', expression)
            if not reference:
                return expression
            name = reference.group(1)
            assignment = re.search(r'^  ' + re.escape(name) + r'\s*=\s*(.*?)(?=^  [a-z_]+\s*=|^\})', REGIONAL, re.M | re.S)
            if not assignment:
                raise AssertionError("Missing GuardDuty local " + name)
            body = re.sub(r'(?m)^\s*#.*$', '', assignment.group(1)).strip()
            expression = expression.replace(reference.group(0), '(' + body + ')')
        raise AssertionError("GuardDuty local expansion exceeded its bound")

    def native_json_value(self, expression):
        expression = re.sub(r'(?m)^\s*#.*$', '', expression)
        expression = re.sub(r'(?<=[\w"\]})])\n(?=\s*["\w][^\n]*=)', ', ', expression)
        with tempfile.TemporaryDirectory(prefix="security-guardduty-native-") as directory:
            result = subprocess.run([os.environ["SECURITY_TEST_TERRAFORM"], "console", "-no-color"],
                input="jsonencode(" + " ".join(expression.splitlines()) + ")\n", text=True,
                capture_output=True, timeout=15, cwd=directory,
                env={"CHECKPOINT_DISABLE": "1", "TF_CLI_CONFIG_FILE": "/dev/null", "TF_IN_AUTOMATION": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(json.loads(result.stdout.strip()))

    def native_guardduty_desired_states(self):
        detector = block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty")
        desired = detector.split("desired_state = ", 1)[1].split("\n  lifecycle", 1)[0]
        desired = self.expanded_guardduty_expression(desired).replace("data.aws_region.current.name", "region")
        return self.native_json_value('{for region in ' + json.dumps(REGIONS) + ' : region => jsondecode(' + desired + ')}')

    @unittest.skipUnless(os.environ.get("SECURITY_TEST_TERRAFORM"), "optional native HCL fixture check; SDK-free regional matrix guard runs above")
    def test_actual_guardduty_desired_state_disables_every_supported_feature_in_all_regions(self):
        states = self.native_guardduty_desired_states()
        self.assertEqual(set(states), set(REGIONS))
        for region, state in states.items():
            with self.subTest(region=region):
                self.assertIs(state["Enable"], True)
                self.assertEqual(state["FindingPublishingFrequency"], "FIFTEEN_MINUTES")
                features = state["Features"]
                expected = GUARDDUTY_BASE_FEATURES | ({"AI_ANALYST"} if region in GUARDDUTY_ANALYST_REGIONS else set())
                self.assertEqual({feature["Name"] for feature in features}, expected)
                self.assertEqual(len(features), len(expected))
                self.assertTrue(all(feature["Status"] == "DISABLED" for feature in features))
                runtime = next(feature for feature in features if feature["Name"] == "RUNTIME_MONITORING")
                self.assertEqual({agent["Name"]: agent["Status"] for agent in runtime["AdditionalConfiguration"]}, {
                    "EKS_ADDON_MANAGEMENT": "DISABLED", "ECS_FARGATE_AGENT_MANAGEMENT": "DISABLED", "EC2_AGENT_MANAGEMENT": "DISABLED"})
                self.assertEqual(len(runtime["AdditionalConfiguration"]), 3)
                self.assertTrue(all("AdditionalConfiguration" not in feature for feature in features if feature["Name"] != "RUNTIME_MONITORING"))
                self.assertNotIn("DataSources", state)
        detector = block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty")
        region_guard = re.search(r'precondition\s*\{\s*condition\s*=(.*?)\n\s*error_message', detector, re.S).group(1)
        region_guard = self.expanded_guardduty_expression(region_guard).replace("data.aws_region.current.name", "region")
        guarded = self.native_json_value('{for region in ' + json.dumps(REGIONS + ["af-south-1", "future-region-1"]) + ' : region => ' + region_guard + '}')
        self.assertEqual(guarded, {**{region: True for region in REGIONS}, "af-south-1": False, "future-region-1": False})

    @unittest.skipUnless(os.environ.get("SECURITY_TEST_TERRAFORM"), "optional native HCL fixture check; SDK-free disabled-feature guard runs above")
    def test_actual_guardduty_feature_readback_allows_only_documented_absence(self):
        states = self.native_guardduty_desired_states()
        detector = block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty")
        conditions = re.findall(r'postcondition\s*\{\s*condition\s*=(.*?)\n\s*error_message\s*=\s*"([^"]+)"', detector, re.S)
        expression = next(value for value, message in conditions if message.startswith("GuardDuty optional"))
        for region in ("us-west-1", "us-east-1"):
            good = states[region]
            fixtures = [("all supported disabled", good, True)]
            for name in [feature["Name"] for feature in good["Features"]]:
                missing = copy.deepcopy(good)
                missing["Features"] = [feature for feature in missing["Features"] if feature["Name"] != name]
                enabled = copy.deepcopy(good)
                next(feature for feature in enabled["Features"] if feature["Name"] == name)["Status"] = "ENABLED"
                fixtures.extend([(name + " missing", missing, False), (name + " enabled", enabled, False)])
            for name in ("AI_ANALYST", "UNREVIEWED_FUTURE_FEATURE"):
                for status in ("DISABLED", "ENABLED"):
                    added = copy.deepcopy(good)
                    added["Features"] = [feature for feature in added["Features"] if feature["Name"] != name]
                    added["Features"].append({"Name": name, "Status": status})
                    fixtures.append((name + " " + status, added, status == "DISABLED"))
            for invalid in (None, [], {}):
                malformed = copy.deepcopy(good)
                malformed["Features"] = invalid
                fixtures.append(("malformed features", malformed, False))
            rendered_cases = {}
            for index, (label, properties, expected) in enumerate(fixtures):
                rendered = self.expanded_guardduty_expression(expression).replace("data.aws_region.current.name", json.dumps(region))
                rendered = rendered.replace("self.properties", json.dumps(json.dumps(properties)))
                rendered_cases[str(index)] = (label, rendered, expected)
            actual = self.native_json_value('{' + ','.join(json.dumps(index) + ' = (' + rendered + ')' for index, (_, rendered, _) in rendered_cases.items()) + '}')
            for index, (label, _, expected) in rendered_cases.items():
                with self.subTest(region=region, case=label):
                    self.assertIs(actual[index], expected)

    def test_guardduty_nested_agent_postcondition_is_strict(self):
        detector = block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty")
        agents = re.search(r'guardduty_runtime_agents\s*=\s*(\[[^]]+\])', REGIONAL)
        self.assertIsNotNone(agents)
        self.assertEqual(set(json.loads(agents.group(1))),
                         {"EKS_ADDON_MANAGEMENT", "ECS_FARGATE_AGENT_MANAGEMENT", "EC2_AGENT_MANAGEMENT"})
        self.assertIn('AdditionalConfiguration = [for agent in local.guardduty_runtime_agents', detector)
        self.assertIn('agent, "MISSING") == "DISABLED"', detector)
        self.assertIn('setting.Status == "DISABLED"', detector)

    @unittest.skipUnless(os.environ.get("SECURITY_TEST_TERRAFORM"), "optional native HCL fixture check; SDK-free static guard runs above")
    def test_actual_guardduty_nested_expression_rejects_enabled_missing_malformed_agents(self):
        detector = block(REGIONAL, "aws_cloudcontrolapi_resource", "guardduty")
        conditions = re.findall(r'postcondition\s*\{\s*condition\s*=(.*?)\n\s*error_message\s*=\s*"([^"]+)"', detector, re.S)
        expression = next(value for value, message in conditions if message.startswith("GuardDuty nested"))
        agents = json.loads(re.search(r'guardduty_runtime_agents\s*=\s*(\[[^]]+\])', REGIONAL).group(1))
        good = {"Features": [{"Name": "RUNTIME_MONITORING", "Status": "DISABLED", "AdditionalConfiguration": [
            {"Name": name, "Status": "DISABLED"} for name in agents]}]}
        fixtures = [("all disabled", good, True)]
        for agent in agents:
            bad = copy.deepcopy(good)
            next(setting for setting in bad["Features"][0]["AdditionalConfiguration"] if setting["Name"] == agent)["Status"] = "ENABLED"
            fixtures.append((agent + " enabled", bad, False))
        for label, settings in (("missing one", good["Features"][0]["AdditionalConfiguration"][:-1]),
                                ("missing all", []), ("malformed", None),
                                ("unknown enabled", good["Features"][0]["AdditionalConfiguration"] + [{"Name": "NEW_AGENT", "Status": "ENABLED"}])):
            bad = copy.deepcopy(good)
            bad["Features"][0]["AdditionalConfiguration"] = settings
            fixtures.append((label, bad, False))
        with tempfile.TemporaryDirectory(prefix="security-expression-") as directory:
            for label, properties, expected in fixtures:
                with self.subTest(case=label):
                    rendered = expression.replace("local.guardduty_runtime_agents", json.dumps(agents)).replace("self.properties", json.dumps(json.dumps(properties)))
                    result = subprocess.run([os.environ["SECURITY_TEST_TERRAFORM"], "console", "-no-color"],
                        input=" ".join(rendered.splitlines()) + "\n", text=True, capture_output=True, timeout=15, cwd=directory,
                        env={"CHECKPOINT_DISABLE": "1", "TF_CLI_CONFIG_FILE": "/dev/null", "TF_IN_AUTOMATION": "1"})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(expected).lower())

    def test_kms_and_backup_tampering_alert_without_routine_cleanup_noise(self):
        pattern = REGIONAL
        def names_for(source):
            match = re.search(r'eventSource\s*=\s*\["' + re.escape(source) +
                              r'"\]\s*eventName\s*=\s*\[([^]]+)\]', pattern)
            self.assertIsNotNone(match)
            return set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertEqual(names_for("kms.amazonaws.com"),
                         {"DisableKey", "ScheduleKeyDeletion", "PutKeyPolicy", "DisableKeyRotation"})
        self.assertEqual(names_for("backup.amazonaws.com"),
                         {"DeleteBackupVault", "DeleteBackupPlan", "DeleteBackupSelection", "UpdateBackupPlan",
                          "PutBackupVaultAccessPolicy", "DeleteBackupVaultAccessPolicy",
                          "PutBackupVaultLockConfiguration", "DeleteBackupVaultLockConfiguration",
                          "UpdateRecoveryPointLifecycle", "UpdateGlobalSettings", "UpdateRegionSettings"})
        deletion = re.search(r'eventName\s*=\s*\["DeleteRecoveryPoint"\]\s*'
                             r'requestParameters\s*=\s*\{\s*backupVaultName\s*=\s*\[([^]]+)\]', pattern)
        self.assertIsNotNone(deletion)
        self.assertEqual(set(re.findall(r'"([^"]+)"', deletion.group(1))),
                         {"ejc3-backup", "ejc3-backup-dr", "fcvm-backups", "ejc3-backup-recovery"})

    def test_all_regional_modules_and_watchdog_coverage_agree(self):
        modules = set(re.findall(r'^module "(security_(?:main|staging)_[^"]+)"', TERRAFORM, re.M))
        self.assertEqual(modules, {"security_" + account + "_" + region.replace("-", "_")
                                  for account in ("main", "staging") for region in REGIONS})
        schedule = block(TERRAFORM, "aws_cloudwatch_event_target", "security_delivery_health")
        for module in modules:
            self.assertIn("module." + module, schedule)

    def test_dlqs_are_regional_encrypted_bounded_and_source_pinned(self):
        for source, queue in ((REGIONAL, "delivery_failures"), (TERRAFORM, "security_delivery_failures")):
            queue_block = block(source, "aws_sqs_queue", queue)
            self.assertIn("1209600", queue_block)
            self.assertRegex(queue_block, r"sqs_managed_sse_enabled\s*=\s*true")
            policy = block(source, "aws_sqs_queue_policy", queue)
            self.assertIn('"sqs:SendMessage"', policy)
            self.assertIn('"aws:SourceArn"', policy)
            self.assertIn('"aws:SecureTransport"', policy)
            self.assertIn('"aws:PrincipalIsAWSService"', policy)
        for source, target in ((REGIONAL, "central"), (TERRAFORM, "security_notify"), (TERRAFORM, "security_delivery_health")):
            self.assertIn("dead_letter_config", block(source, "aws_cloudwatch_event_target", target))

    def test_retry_policy_is_only_configured_for_supported_target_types(self):
        target = block(REGIONAL, "aws_cloudwatch_event_target", "central")
        self.assertNotRegex(target, r"\bretry_policy\s*\{")
        self.assertRegex(target, r"\barn\s*=\s*var\.central_event_bus_arn\b")
        self.assertRegex(target, r"\brole_arn\s*=\s*var\.forwarding_role_arn\b")
        self.assertIn("aws_sqs_queue.delivery_failures.arn", target)
        for name, age, attempts in (("security_notify", 86400, 185), ("security_delivery_health", 300, 2)):
            target = block(TERRAFORM, "aws_cloudwatch_event_target", name)
            self.assertEqual(len(re.findall(r"\bretry_policy\s*\{", target)), 1)
            self.assertRegex(target, rf"maximum_event_age_in_seconds\s*=\s*{age}\b")
            self.assertRegex(target, rf"maximum_retry_attempts\s*=\s*{attempts}\b")
            self.assertIn("aws_sqs_queue.security_delivery_failures.arn", target)

    def test_failure_alarms_trigger_on_one_failure_and_missing_pulses_alarm(self):
        gauges = block(TERRAFORM, "aws_cloudwatch_metric_alarm", "security_delivery")
        self.assertIn('"FailedDeliveries", "QueuedEvents", "ObserverErrors"', gauges)
        self.assertRegex(gauges, r"threshold\s*=\s*1\b")
        self.assertRegex(gauges, r"evaluation_periods\s*=\s*1\b")
        heartbeat = block(TERRAFORM, "aws_cloudwatch_metric_alarm", "security_delivery_heartbeat")
        self.assertIn('"FILL(pulse, 0)"', heartbeat)
        self.assertRegex(heartbeat, r"period\s*=\s*300\b")
        self.assertRegex(heartbeat, r"evaluation_periods\s*=\s*3\b")
        self.assertRegex(heartbeat, r"datapoints_to_alarm\s*=\s*3\b")
        self.assertRegex(heartbeat, r'treat_missing_data\s*=\s*"breaching"')
        self.assertIn('"LessThanThreshold"', heartbeat)
        self.assertRegex(heartbeat, r"threshold\s*=\s*1\b")

    def test_observer_is_read_only_except_own_logs_and_health_metrics(self):
        main = block(TERRAFORM, "aws_iam_role_policy", "security_delivery_health")
        staging = block(TERRAFORM, "aws_iam_role_policy", "security_delivery_observer_staging")
        for policy in (main, staging):
            self.assertNotIn("AdministratorAccess", policy)
            self.assertNotIn("sqs:ReceiveMessage", policy)
            self.assertNotIn("sqs:DeleteMessage", policy)
            self.assertNotIn("sqs:PurgeQueue", policy)
            self.assertIn('"sqs:GetQueueAttributes"', policy)
            self.assertIn('"cloudwatch:GetMetricData"', policy)
            self.assertIn('"events:DescribeRule", "events:ListTargetsByRule"', policy)
            self.assertIn('["security-forward-findings", "security-forward-audit"]', policy)
            self.assertIn(':rule/${rule}', policy)
            self.assertNotIn('"events:Put', policy)
        self.assertIn('aws_cloudwatch_event_rule.security_notify.arn', main)
        self.assertIn('aws_cloudwatch_event_rule.security_delivery_health.arn', main)
        self.assertNotIn('"sts:AssumeRole"', staging)
        self.assertIn('"cloudwatch:namespace" = "SecurityDelivery"', main)
        role = block(TERRAFORM, "aws_iam_role", "security_delivery_observer_staging")
        self.assertIn("AWS = aws_iam_role.security_delivery_health.arn", role)

    def test_runtime_and_log_retention_remain_bounded(self):
        function = block(TERRAFORM, "aws_lambda_function", "security_delivery_health")
        self.assertRegex(function, r"timeout\s*=\s*90\b")
        self.assertRegex(function, r"reserved_concurrent_executions\s*=\s*1\b")
        self.assertRegex(block(TERRAFORM, "aws_cloudwatch_log_group", "security_delivery_health"),
                         r"retention_in_days\s*=\s*30\b")
        self.assertIn('schedule_expression = "rate(5 minutes)"', block(TERRAFORM, "aws_cloudwatch_event_rule", "security_delivery_health"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

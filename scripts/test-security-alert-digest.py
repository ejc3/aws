#!/usr/bin/env python3
"""scripts/security-alert-digest.py and security-alert-digest.tf: security alerts become a few readable emails.

Offline: an in-memory DynamoDB that evaluates the function's own condition and update expressions, a fake SNS, and
the Terraform source. Run: python3 -S -B scripts/test-security-alert-digest.py
"""
import copy
import importlib.util
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.update({"STATE_TABLE": "digest", "SNS_TOPIC_ARN": "arn:aws:sns:us-west-1:111111111111:cost-alerts",
                   "ACCOUNT_NAMES": json.dumps({"111111111111": "main"})})
sys.modules.setdefault("boto3", types.ModuleType("boto3"))
SPEC = importlib.util.spec_from_file_location("digest", ROOT / "scripts" / "security-alert-digest.py")
digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(digest)
TF = (ROOT / "security-alert-digest.tf").read_text()
MONITORING = (ROOT / "security-monitoring.tf").read_text()
MAIN = "111111111111"
T0 = 1_800_000_000


class AwsError(Exception):
    def __init__(self, code, reasons=None):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}
        if reasons is not None:
            self.response["CancellationReasons"] = reasons


def value(attr):
    return int(attr["N"]) if "N" in attr else attr["S"]


class Expr:
    """Just enough of DynamoDB's expression grammar for this function: attribute_not_exists, < <= =, AND, OR,
    parentheses; SET a = :v, b = if_not_exists(b, :v); begins_with in key conditions."""
    TOKEN = re.compile(r"\s*(\(|\)|,|<=|<|=|[A-Za-z_][A-Za-z_0-9]*|:[A-Za-z_0-9]+)")

    @classmethod
    def tokens(cls, text):
        out, pos = [], 0
        while pos < len(text.rstrip()):
            match = cls.TOKEN.match(text, pos)
            assert match, "unsupported expression: " + text[pos:]
            out.append(match.group(1))
            pos = match.end()
        return out

    @classmethod
    def condition(cls, text, item, values):
        tokens = cls.tokens(text)

        def operand(tok):
            if tok.startswith(":"):
                return value(values[tok])
            return value(item[tok]) if tok in item else None

        def primary(i):
            if tokens[i] == "(":
                result, i = disjunction(i + 1)
                assert tokens[i] == ")"
                return result, i + 1
            if tokens[i] in ("attribute_not_exists", "attribute_exists", "begins_with"):
                fn = tokens[i]
                assert tokens[i + 1] == "("
                if fn == "begins_with":
                    left, right = operand(tokens[i + 2]), operand(tokens[i + 4])
                    return left is not None and str(left).startswith(right), i + 6
                present = tokens[i + 2] in item
                return (not present) if fn == "attribute_not_exists" else present, i + 4
            left, op, right = operand(tokens[i]), tokens[i + 1], operand(tokens[i + 2])
            if left is None or right is None:
                return False, i + 3
            return {"<": left < right, "<=": left <= right, "=": left == right}[op], i + 3

        def conjunction(i):
            result, i = primary(i)
            while i < len(tokens) and tokens[i] == "AND":
                other, i = primary(i + 1)
                result = result and other
            return result, i

        def disjunction(i):
            result, i = conjunction(i)
            while i < len(tokens) and tokens[i] == "OR":
                other, i = conjunction(i + 1)
                result = result or other
            return result, i

        result, end = disjunction(0)
        assert end == len(tokens), "trailing tokens in " + text
        return result

    @classmethod
    def update(cls, text, item, values):
        assert text.startswith("SET ")
        for clause in re.split(r",\s*(?![^()]*\))", text[4:]):
            name, expression = [part.strip() for part in clause.split("=", 1)]
            match = re.fullmatch(r"if_not_exists\((\w+), (:\w+)\)", expression)
            if match:
                item[name] = copy.deepcopy(item.get(match.group(1), values[match.group(2)]))
            else:
                assert expression.startswith(":"), expression
                item[name] = copy.deepcopy(values[expression])


class FakeDDB:
    def __init__(self):
        self.items, self.calls = {}, []
        self.fail = {}  # operation -> exception raised on every call

    def _key(self, key):
        return key["pk"]["S"], key["sk"]["S"]

    def _maybe_fail(self, operation):
        self.calls.append(operation)
        if operation in self.fail:
            raise self.fail[operation]

    def transact_write_items(self, TransactItems):
        self._maybe_fail("transact_write_items")
        staged = dict(self.items)  # copy-on-write: only the touched items are copied
        reasons, failed = [], False
        for entry in TransactItems:
            if "Update" in entry:
                op = entry["Update"]
                key = self._key(op["Key"])
                item = copy.deepcopy(staged.get(key, {"pk": op["Key"]["pk"], "sk": op["Key"]["sk"]}))
                if "ConditionExpression" in op and not Expr.condition(op["ConditionExpression"], item, op["ExpressionAttributeValues"]):
                    reasons.append({"Code": "ConditionalCheckFailed"})
                    failed = True
                    continue
                Expr.update(op["UpdateExpression"], item, op["ExpressionAttributeValues"])
                staged[key] = item
            else:
                op = entry["Put"]
                staged[self._key(op["Item"])] = copy.deepcopy(op["Item"])
            reasons.append({"Code": "None"})
        if failed:
            raise AwsError("TransactionCanceledException", reasons)
        self.items = staged

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None, ReturnValues=None):
        self._maybe_fail("update_item")
        key = self._key(Key)
        item = copy.deepcopy(self.items.get(key, {"pk": Key["pk"], "sk": Key["sk"]}))
        if ConditionExpression and not Expr.condition(ConditionExpression, item, ExpressionAttributeValues):
            raise AwsError("ConditionalCheckFailedException")
        Expr.update(UpdateExpression, item, ExpressionAttributeValues)
        self.items[key] = item
        return {"Attributes": copy.deepcopy(item)} if ReturnValues == "ALL_NEW" else {}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, ConsistentRead, Limit, ExclusiveStartKey=None):
        self._maybe_fail("query")
        assert ConsistentRead is True and 1 <= Limit <= 1000
        # The partition first (DynamoDB never reads other partitions), then the key condition on each candidate.
        partition = ExpressionAttributeValues[":pk"]["S"]
        assert KeyConditionExpression.startswith("pk = :pk")
        matching = sorted((key, item) for key, item in self.items.items()
                          if key[0] == partition and Expr.condition(KeyConditionExpression, item, ExpressionAttributeValues))
        if ExclusiveStartKey:
            start = self._key(ExclusiveStartKey)
            matching = [(key, item) for key, item in matching if key > start]
        page = matching[:Limit]
        response = {"Items": [copy.deepcopy(item) for _, item in page]}
        if len(matching) > Limit:
            response["LastEvaluatedKey"] = {"pk": page[-1][1]["pk"], "sk": page[-1][1]["sk"]}
        return response

    def put_item(self, TableName, Item):
        self._maybe_fail("put_item")
        self.items[self._key(Item)] = copy.deepcopy(Item)
        return {}

    def batch_write_item(self, RequestItems):
        self._maybe_fail("batch_write_item")
        (requests,) = RequestItems.values()
        assert len(requests) <= 25
        for request in requests:
            self.items.pop(self._key(request["DeleteRequest"]["Key"]), None)
        return {"UnprocessedItems": {}}

    def pending(self, prefix=""):
        return sorted(key[1] for key in self.items if key[0] == "pending" and key[1].startswith(prefix))


class FakeSNS:
    def __init__(self):
        self.messages, self.error = [], None

    def publish(self, TopicArn, Subject, Message):
        if self.error:
            raise self.error
        assert TopicArn == os.environ["SNS_TOPIC_ARN"]
        assert len(Subject) < 100 and all(32 <= ord(c) < 127 for c in Subject), Subject
        assert len(Message.encode()) < 256 * 1024
        self.messages.append({"at": CLOCK[0], "subject": Subject, "body": Message})
        return {"MessageId": "m"}


CLOCK = [T0]


def guardduty(finding_id="gd-1", severity=8.0, title="Unusual API call", account=MAIN, region="us-west-1"):
    return {"id": "evt-" + finding_id, "source": "aws.guardduty", "detail-type": "GuardDuty Finding", "account": account,
            "region": region, "detail": {"id": finding_id, "severity": severity, "title": title, "type": "Recon:IAMUser/Unusual",
                                         "accountId": account, "region": region, "service": {"archived": False},
                                         "resource": {"resourceType": "AccessKey", "accessKeyDetails": {"userName": "builder"}}}}


def inspector(n, severity="HIGH", cve=None, resource="i-0abc", account=MAIN):
    arn = "arn:aws:inspector2:us-west-1:%s:finding/%032x" % (account, n)
    return {"id": "evt-ins-%d" % n, "source": "aws.inspector2", "detail-type": "Inspector2 Finding", "account": account,
            "region": "us-west-1", "detail": {"findingArn": arn, "title": "CVE-2026-%04d - openssl" % (n % 50), "severity": severity,
                                              "status": "ACTIVE", "type": "PACKAGE_VULNERABILITY", "awsAccountId": account,
                                              "resources": [{"id": resource, "type": "AWS_EC2_INSTANCE"}],
                                              "packageVulnerabilityDetails": {"vulnerabilityId": cve or "CVE-2026-%04d" % (n % 50)}}}


def securityhub(finding_id, control="EC2.9", severity="HIGH", workflow="NEW", record="ACTIVE", product="securityhub"):
    return {"id": "evt-" + finding_id, "source": "aws.securityhub", "detail-type": "Security Hub Findings - Imported",
            "account": MAIN, "region": "us-west-1",
            "detail": {"findings": [{"Id": finding_id, "ProductArn": "arn:aws:securityhub:us-west-1::product/aws/" + product,
                                     "Title": control + " instances should not have a public IPv4 address", "AwsAccountId": MAIN,
                                     "Region": "us-west-1", "Severity": {"Label": severity}, "RecordState": record,
                                     "Workflow": {"Status": workflow}, "Compliance": {"SecurityControlId": control},
                                     "Resources": [{"Id": "arn:aws:ec2:us-west-1:%s:instance/i-1" % MAIN}]}]}}


def cloudtrail(event_id, name="AttachRolePolicy", source="iam.amazonaws.com", identity_type="AssumedRole"):
    return {"id": "evt-" + event_id, "source": "aws." + source.split(".")[0], "detail-type": "AWS API Call via CloudTrail",
            "account": MAIN, "region": "us-east-1",
            "detail": {"eventID": event_id, "eventName": name, "eventSource": source, "awsRegion": "us-east-1",
                       "recipientAccountId": MAIN, "sourceIPAddress": "192.0.2.10",
                       "userIdentity": {"type": identity_type, "arn": "arn:aws:sts::%s:assumed-role/admin/x" % MAIN},
                       "requestParameters": {"roleName": "builder", "policyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}}}


def access_analyzer(finding_id="aa-1"):
    return {"id": "evt-" + finding_id, "source": "aws.access-analyzer", "detail-type": "Access Analyzer Finding",
            "account": MAIN, "region": "us-west-1",
            "detail": {"id": finding_id, "status": "ACTIVE", "resourceType": "AWS::S3::Bucket", "resource": "arn:aws:s3:::bucket",
                       "isPublic": True, "accountId": MAIN, "region": "us-west-1", "principal": {"AWS": "*"}}}


def sqs_batch(events, start=0):
    return {"Records": [{"messageId": "m%d" % (start + i), "body": json.dumps(event)} for i, event in enumerate(events)]}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.ddb, self.sns = FakeDDB(), FakeSNS()
        digest._ddb, digest._sns = self.ddb, self.sns
        CLOCK[0] = T0
        digest.now = lambda: CLOCK[0]
        digest.time.sleep = lambda seconds: None
        self.logs = []
        digest.print = lambda *args, **kwargs: self.logs.append(" ".join(map(str, args)))

    def deliver(self, events, start=0):
        return digest.lambda_handler(sqs_batch(events, start), None)

    def tick(self, seconds=60):
        CLOCK[0] += seconds
        return digest.lambda_handler({"source": "aws.events", "detail-type": "Scheduled Event"}, None)

    # --- dedupe -------------------------------------------------------------------------------------------------

    def test_a_second_event_for_the_same_finding_sends_nothing(self):
        self.deliver([guardduty("gd-1")])
        self.assertEqual(len(self.sns.messages), 1)
        for _ in range(5):
            self.tick(120)
            self.deliver([guardduty("gd-1", title="Unusual API call (count 7)")])
        self.tick(120)
        self.assertEqual(len(self.sns.messages), 1)
        self.assertEqual(self.ddb.pending(), [])

    def test_updates_keep_a_live_finding_quiet_and_a_reopened_one_mails_again_after_a_week(self):
        self.deliver([inspector(1)])
        self.tick(700)
        self.assertEqual(len(self.sns.messages), 1)
        for _ in range(10):                      # re-imported daily for ten days: still one email
            self.tick(86400)
            self.deliver([inspector(1)])
            self.tick(700)
        self.assertEqual(len(self.sns.messages), 1)
        self.tick(8 * 86400)                     # resolved, silent for eight days, then back
        self.deliver([inspector(1)])
        self.tick(700)
        self.assertEqual(len(self.sns.messages), 2)

    def test_the_identity_covers_each_source(self):
        identities = {
            digest.alerts_from_event(guardduty("g"))[0]["identity"]: "guardduty",
            digest.alerts_from_event(inspector(3))[0]["identity"]: "inspector",
            digest.alerts_from_event(securityhub("arn:aws:securityhub:x:finding/1"))[0]["identity"]: "securityhub",
            digest.alerts_from_event(access_analyzer("a"))[0]["identity"]: "access-analyzer",
            digest.alerts_from_event(cloudtrail("c"))[0]["identity"]: "cloudtrail",
        }
        self.assertEqual(sorted(identities.values()), ["access-analyzer", "cloudtrail", "guardduty", "inspector", "securityhub"])
        self.assertIn("guardduty:%s:us-west-1:g" % MAIN, identities)
        self.assertIn("inspector:arn:aws:inspector2:us-west-1:%s:finding/%032x" % (MAIN, 3), identities)
        self.assertIn("securityhub:arn:aws:securityhub:x:finding/1", identities)
        self.assertIn("cloudtrail:%s:c" % MAIN, identities)
        # Two EventBridge deliveries of one finding (different event ids) are the same alert.
        a, b = guardduty("same"), guardduty("same")
        b["id"] = "another-event-id"
        self.assertEqual(digest.alerts_from_event(a)[0]["identity"], digest.alerts_from_event(b)[0]["identity"])

    def test_only_the_securityhub_findings_that_match_the_rule_are_recorded(self):
        event = securityhub("f1")
        event["detail"]["findings"].append(dict(event["detail"]["findings"][0], Id="f2", Severity={"Label": "LOW"}))
        event["detail"]["findings"].append(dict(event["detail"]["findings"][0], Id="f3", Workflow={"Status": "SUPPRESSED"}))
        event["detail"]["findings"].append(dict(event["detail"]["findings"][0], Id="f4", RecordState="ARCHIVED"))
        self.assertEqual([a["identity"] for a in digest.alerts_from_event(event)], ["securityhub:f1"])

    def test_native_copies_in_a_securityhub_event_are_dropped_but_a_member_inspector_copy_is_kept(self):
        event = securityhub("f1")
        base = event["detail"]["findings"][0]
        gd = "arn:aws:securityhub:us-west-1::product/aws/guardduty"
        insp = "arn:aws:securityhub:us-west-1::product/aws/inspector"
        event["detail"]["findings"] += [dict(base, Id="own-inspector", ProductArn=insp),
                                        dict(base, Id="member-guardduty", ProductArn=gd, AwsAccountId="222222222222"),
                                        dict(base, Id="member-inspector", ProductArn=insp, AwsAccountId="222222222222")]
        self.assertEqual([a["identity"] for a in digest.alerts_from_event(event)],
                         ["securityhub:f1", "securityhub:member-inspector"])

    # --- classification ----------------------------------------------------------------------------------------

    def test_immediate_and_digest_classification(self):
        immediate = [guardduty("g"), cloudtrail("c"), access_analyzer("a"),
                     cloudtrail("r", name="ConsoleLogin", source="signin.amazonaws.com", identity_type="Root")]
        member_inspector = securityhub("dolphin-inspector", product="inspector")
        member_inspector["detail"]["findings"][0]["AwsAccountId"] = "222222222222"  # a member account's copy
        later = [inspector(1), securityhub("s"), member_inspector,
                 {"id": "e", "source": "aws.health", "detail-type": "AWS Health Event", "account": MAIN, "region": "us-west-1", "detail": {}},
                 guardduty("low", severity=2.0)]
        for event in immediate:
            with self.subTest(source=event["source"]):
                self.assertEqual(digest.alerts_from_event(event)[0]["kind"], digest.IMMEDIATE)
        for event in later:
            with self.subTest(source=event["source"]):
                self.assertEqual(digest.alerts_from_event(event)[0]["kind"], digest.DIGEST)
        self.assertEqual(digest.alerts_from_event(later[2])[0]["source"], "Inspector")

    def test_an_immediate_alert_goes_out_at_once_and_a_digest_waits(self):
        self.deliver([guardduty("g"), inspector(1)])
        self.assertEqual(len(self.sns.messages), 1)
        self.assertIn("GuardDuty", self.sns.messages[0]["subject"])
        self.assertNotIn("openssl", self.sns.messages[0]["body"])
        self.assertNotIn("DIGEST", self.sns.messages[0]["body"])
        self.assertEqual(self.ddb.pending(), ["d#inspector:arn:aws:inspector2:us-west-1:%s:finding/%032x" % (MAIN, 1)])
        self.tick(60)
        self.assertEqual(len(self.sns.messages), 1)       # still settling
        self.tick(120)
        self.assertEqual(len(self.sns.messages), 2)       # quiet for two minutes: the digest goes
        self.assertIn("DIGEST: 1 new finding", self.sns.messages[1]["body"])
        self.assertEqual(self.ddb.pending(), [])

    def test_a_single_immediate_alert_is_readable_text(self):
        self.deliver([cloudtrail("evt-123")])
        message = self.sns.messages[0]
        self.assertEqual(message["subject"], "Security alert: CloudTrail: AttachRolePolicy (iam.amazonaws.com)")
        body = message["body"]
        for expected in ("[CloudTrail] AttachRolePolicy (iam.amazonaws.com)", "Account: %s (main)" % MAIN, "Region: us-east-1",
                         "Resource: builder", "by arn:aws:sts::%s:assumed-role/admin/x from 192.0.2.10" % MAIN,
                         "Console: https://us-east-1.console.aws.amazon.com/cloudtrailv2/home?region=us-east-1#/events/evt-123"):
            self.assertIn(expected, body)
        self.assertFalse(body.lstrip().startswith("{"), "not raw JSON")

    # --- rate cap -------------------------------------------------------------------------------------------------

    def test_a_burst_of_2000_inspector_findings_is_at_most_two_messages(self):
        # The 2026-10-19 rescan: about 2,000 events in five minutes, arriving in SQS batches of 100.
        for batch in range(20):
            CLOCK[0] = T0 + batch * 15
            self.deliver([inspector(batch * 100 + i, resource="i-%d" % (i % 3)) for i in range(100)], start=batch * 100)
        for _ in range(30):                               # half an hour of the one-minute schedule
            self.tick(60)
        self.assertLessEqual(len(self.sns.messages), 2)
        self.assertGreaterEqual(len(self.sns.messages), 1)
        total = sum(int(re.search(r"Total in this message: (\d+)", m["body"]).group(1)) for m in self.sns.messages)
        self.assertEqual(total, 2000)
        self.assertEqual(self.ddb.pending(), [])
        # A replay of the whole burst (the next rescan) mails nothing.
        self.deliver([inspector(i) for i in range(2000)], start=5000)
        for _ in range(15):
            self.tick(60)
        self.assertLessEqual(len(self.sns.messages), 2)

    def test_never_more_than_one_message_a_minute_even_for_a_flood_of_immediate_alerts(self):
        for second in range(0, 600, 5):                   # 2,400 immediate alerts over ten minutes
            CLOCK[0] = T0 + second
            self.deliver([cloudtrail("c-%d-%d" % (second, i)) for i in range(20)], start=second * 100)
            if second % 60 == 0:
                digest.lambda_handler({"source": "aws.events"}, None)
        for _ in range(5):
            self.tick(60)
        times = [m["at"] for m in self.sns.messages]
        self.assertTrue(all(b - a >= 60 for a, b in zip(times, times[1:])), times)
        self.assertLessEqual(len(times), 12)
        self.assertEqual(self.ddb.pending(), [])
        self.assertIn("more immediate alerts", self.sns.messages[1]["body"])

    def test_at_most_one_digest_every_ten_minutes(self):
        self.deliver([inspector(1)])
        while not self.sns.messages:
            self.tick(60)
        first = self.sns.messages[0]["at"]
        self.deliver([inspector(2)])
        while CLOCK[0] + 60 < first + digest.DIGEST_WINDOW:
            self.tick(60)
        self.assertEqual(len(self.sns.messages), 1)
        for _ in range(3):
            self.tick(60)
        self.assertEqual(len(self.sns.messages), 2)
        self.assertGreaterEqual(self.sns.messages[1]["at"] - first, digest.DIGEST_WINDOW)

    def test_a_concurrent_run_cannot_send_while_another_holds_the_lease(self):
        self.deliver([inspector(1)])
        CLOCK[0] += 300
        self.ddb.update_item(TableName="digest", Key=digest.CONTROL, UpdateExpression="SET lease_until = :l",
                             ExpressionAttributeValues={":l": {"N": str(CLOCK[0] + 100)}})
        self.assertEqual(digest.flush(CLOCK[0])["sent"], 0)
        self.assertEqual(self.sns.messages, [])
        CLOCK[0] += 101                                   # the holder died; its lease expires
        self.assertEqual(digest.flush(CLOCK[0])["sent"], 1)

    # --- digest content ----------------------------------------------------------------------------------------

    def test_digest_summarises_by_source_severity_control_and_resource(self):
        events = [inspector(i, severity="CRITICAL" if i < 3 else "HIGH", cve="CVE-2026-0001" if i < 40 else "CVE-2026-0002",
                            resource="i-web" if i % 2 else "i-db") for i in range(60)]
        events += [securityhub("sh-%d" % i, control="EC2.9") for i in range(5)]
        self.deliver(events)
        self.tick(180)
        self.assertEqual(len(self.sns.messages), 1)
        message = self.sns.messages[0]
        self.assertEqual(message["subject"], "Security alerts: 65 in digest")
        body = message["body"]
        self.assertIn("DIGEST: 65 new findings since", body)
        self.assertRegex(body, r"By source:\n\s+60  Inspector\n\s+5  Security Hub")
        self.assertRegex(body, r"By severity:\n\s+62  HIGH\n\s+3  CRITICAL")
        self.assertRegex(body, r"By control or CVE:\n\s+40  CVE-2026-0001\n\s+20  CVE-2026-0002\n\s+5  EC2.9")
        resources = body.split("By resource:\n")[1].split("By account:")[0]
        self.assertRegex(resources, r"^\s+30  i-(web|db)\n\s+30  i-(web|db)\n\s+5  arn:aws:ec2:us-west-1:%s:instance/i-1\n$" % MAIN)
        self.assertIn("First 25:", body)
        self.assertIn("... and 40 more.", body)
        first = body.split("First 25:\n")[1].splitlines()
        self.assertTrue(first[0].startswith("  - [CRITICAL] Inspector:"), first[0])
        self.assertEqual(len([line for line in first if line.startswith("  - ")]), 25)
        self.assertIn("https://us-west-1.console.aws.amazon.com/inspector/v2/home?region=us-west-1#/findings/all", body)

    def test_subjects_are_ascii_and_short_and_fields_are_one_line(self):
        event = guardduty("g", title="Bad thing — " + "x" * 300 + "\nsecond line")
        self.deliver([event])
        subject = self.sns.messages[0]["subject"]
        self.assertLess(len(subject), 100)
        self.assertTrue(all(32 <= ord(c) < 127 for c in subject))
        self.assertNotIn("\nsecond line", self.sns.messages[0]["body"])

    # --- failures -----------------------------------------------------------------------------------------------

    def test_a_failed_publish_loses_nothing_and_the_next_run_sends(self):
        self.sns.error = RuntimeError("SNS throttled")
        self.deliver([guardduty("g")])                   # ingest succeeds; the send fails quietly here
        self.assertEqual(self.ddb.pending(), ["i#guardduty:%s:us-west-1:g" % MAIN])
        with self.assertRaises(RuntimeError):
            self.tick(60)                                 # the scheduled run fails loudly (error alarm)
        self.assertEqual(self.ddb.pending(), ["i#guardduty:%s:us-west-1:g" % MAIN])
        self.sns.error = None
        self.tick(60)
        self.assertEqual(len(self.sns.messages), 1)
        self.assertEqual(self.ddb.pending(), [])

    def test_a_record_that_cannot_be_stored_is_returned_to_the_queue(self):
        self.ddb.fail["transact_write_items"] = AwsError("ProvisionedThroughputExceededException")
        result = self.deliver([guardduty("g"), inspector(1)])
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m0"}, {"itemIdentifier": "m1"}]})
        self.assertEqual(self.sns.messages, [])
        del self.ddb.fail["transact_write_items"]
        self.assertEqual(self.deliver([guardduty("g")]), {"batchItemFailures": []})
        self.assertEqual(len(self.sns.messages), 1)

    def test_a_malformed_record_fails_alone(self):
        batch = sqs_batch([guardduty("g")])
        batch["Records"].append({"messageId": "bad", "body": "not json"})
        self.assertEqual(digest.lambda_handler(batch, None), {"batchItemFailures": [{"itemIdentifier": "bad"}]})
        self.assertEqual(len(self.sns.messages), 1)

    def test_a_transaction_conflict_is_not_mistaken_for_already_seen(self):
        self.ddb.fail["transact_write_items"] = AwsError("TransactionCanceledException", [{"Code": "TransactionConflict"}, {"Code": "None"}])
        self.assertEqual(self.deliver([guardduty("g")]), {"batchItemFailures": [{"itemIdentifier": "m0"}]})

    def test_a_failed_delete_after_publish_repeats_rather_than_drops(self):
        self.ddb.fail["batch_write_item"] = AwsError("InternalServerError")
        self.deliver([guardduty("g")])
        self.assertEqual(len(self.sns.messages), 1)
        del self.ddb.fail["batch_write_item"]
        self.tick(60)
        self.assertEqual(len(self.sns.messages), 2)      # sent again, never lost
        self.tick(60)
        self.assertEqual(len(self.sns.messages), 2)


SNAP = "snap-0123456789abcdef0"
POOL = "905400001560"


class FakeCloudTrail:
    def __init__(self, adds):
        self.adds = adds  # [(principal, invokedBy)]

    def get_paginator(self, name):
        assert name == "lookup_events"
        adds = self.adds

        class Pager:
            def paginate(self, **kwargs):
                assert kwargs["LookupAttributes"] == [{"AttributeKey": "ResourceName", "AttributeValue": SNAP}]
                events = [{"EventName": "ModifySnapshotAttribute", "CloudTrailEvent": json.dumps({
                    "userIdentity": {"invokedBy": by} if by else {"arn": "arn:aws:iam::111111111111:user/someone"},
                    "requestParameters": {"snapshotId": SNAP, "createVolumePermission": {"add": {"items": [{"userId": who}]}}}})}
                    for who, by in adds]
                events.append({"EventName": "CopySnapshot", "CloudTrailEvent": "{}"})
                return [{"Events": events}]
        return Pager()


class FakeEC2:
    def __init__(self, snapshot, shares):
        self.snapshot, self.shares = snapshot, shares

    def describe_snapshots(self, SnapshotIds, OwnerIds):
        if self.snapshot is None:
            raise AwsError("InvalidSnapshot.NotFound")
        return {"Snapshots": [self.snapshot]}

    def describe_snapshot_attribute(self, SnapshotId, Attribute):
        return {"CreateVolumePermissions": [{"UserId": u} for u in self.shares]}


def backup_copy(**over):
    snap = {"SnapshotId": SNAP, "Description": "This snapshot is copied by the AWS Backup service.",
            "Tags": [{"Key": "aws:backup:source-resource", "Value": "x"}, {"Key": "BackupPipeline", "Value": "fleet-processing-v2"}]}
    snap.update(over)
    return snap


def snapshot_finding(principal=POOL, region="us-east-1", account=MAIN, public=False, fid="aa-1"):
    return {"source": "aws.access-analyzer", "detail-type": "Access Analyzer Finding", "id": fid, "account": account,
            "region": region, "detail": {"id": fid, "accountId": account, "region": region, "isPublic": public,
                                         "resourceType": "AWS::EC2::Snapshot", "principal": {"AWS": principal},
                                         "resource": "arn:aws:ec2:%s::snapshot/%s" % (region, SNAP), "status": "ACTIVE"}}


class BackupCopyShareTests(DigestTests):
    """#577: AWS Backup's own air-gapped-copy snapshot share is held, proven and dropped; anything else is mailed."""

    def setUp(self):
        super().setUp()
        digest.BACKUP_COPY_ACCOUNT, digest.BACKUP_COPY_REGIONS = MAIN, {"us-west-1", "us-east-1"}
        self.use(FakeCloudTrail([(POOL, "backup.amazonaws.com")]), FakeEC2(backup_copy(), [POOL]))

    def use(self, trail, ec2):
        digest.boto3.client = lambda name, region_name=None: {"cloudtrail": trail, "ec2": ec2}[name]

    def mailed_after_hold(self):
        self.deliver([snapshot_finding()])
        before = len(self.sns.messages)
        self.tick(digest.HOLD + 60)
        return len(self.sns.messages) - before

    def test_held_not_mailed_at_once(self):
        self.deliver([snapshot_finding()])
        self.tick(60)
        self.assertEqual(self.sns.messages, [])

    def test_aws_backups_own_share_is_dropped(self):
        self.assertEqual(self.mailed_after_hold(), 0)
        self.assertTrue(any("aws-backup-copy-share" in line for line in self.logs))

    def test_a_snapshot_already_deleted_after_the_copy_is_dropped(self):
        self.use(FakeCloudTrail([(POOL, "backup.amazonaws.com")]), FakeEC2(None, []))
        self.assertEqual(self.mailed_after_hold(), 0)

    def test_a_share_made_by_a_person_is_mailed(self):
        self.use(FakeCloudTrail([(POOL, None)]), FakeEC2(backup_copy(), [POOL]))
        self.assertEqual(self.mailed_after_hold(), 1)

    def test_a_person_adding_the_same_principal_after_aws_backup_is_mailed(self):
        self.use(FakeCloudTrail([(POOL, "backup.amazonaws.com"), (POOL, None)]), FakeEC2(backup_copy(), [POOL]))
        self.assertEqual(self.mailed_after_hold(), 1)

    def test_no_cloudtrail_evidence_is_mailed(self):
        self.use(FakeCloudTrail([]), FakeEC2(backup_copy(), [POOL]))
        self.assertEqual(self.mailed_after_hold(), 1)

    def test_a_snapshot_that_is_not_a_backup_copy_is_mailed(self):
        self.use(FakeCloudTrail([(POOL, "backup.amazonaws.com")]), FakeEC2(backup_copy(Description="mine", Tags=[]), [POOL]))
        self.assertEqual(self.mailed_after_hold(), 1)

    def test_an_extra_share_to_another_account_is_mailed(self):
        self.use(FakeCloudTrail([(POOL, "backup.amazonaws.com")]), FakeEC2(backup_copy(), [POOL, "222222222222"]))
        self.assertEqual(self.mailed_after_hold(), 1)

    def test_a_failing_check_is_mailed(self):
        def boom(name, region_name=None):
            raise AwsError("AccessDenied")
        digest.boto3.client = boom
        self.assertEqual(self.mailed_after_hold(), 1)

    def test_public_other_region_other_account_and_other_types_are_not_held(self):
        for event in (snapshot_finding(public=True, fid="p"), snapshot_finding(region="eu-west-1", fid="r"),
                      snapshot_finding(account="222222222222", fid="a")):
            self.assertEqual(digest.alerts_from_event(event)[0]["kind"], digest.IMMEDIATE)
        other = snapshot_finding(fid="b"); other["detail"]["resourceType"] = "AWS::S3::Bucket"
        self.assertEqual(digest.alerts_from_event(other)[0]["kind"], digest.IMMEDIATE)

    def test_a_hold_past_its_limit_is_mailed_whatever_the_check(self):
        self.deliver([snapshot_finding()])
        CLOCK[0] += digest.HOLD_MAX + 60
        before = len(self.sns.messages)
        self.tick(60)
        self.assertEqual(len(self.sns.messages) - before, 1)


class TerraformTests(unittest.TestCase):
    def block(self, source, kind, name):
        start = source.index('resource "%s" "%s" {' % (kind, name))
        depth = 0
        for index in range(source.index("{", start), len(source)):
            depth += (source[index] == "{") - (source[index] == "}")
            if depth == 0:
                return source[start:index + 1]
        raise AssertionError("unclosed")

    def test_the_rule_delivers_into_the_inbox_and_its_pattern_is_unchanged(self):
        target = self.block(MONITORING, "aws_cloudwatch_event_target", "security_notify")
        self.assertIn("arn            = aws_sqs_queue.security_alert_digest_inbox.arn", target)
        self.assertNotIn("cost_alerts", target)
        self.assertIn("dead_letter_config { arn = aws_sqs_queue.security_delivery_failures.arn }", target)
        self.assertIn("create_before_destroy = true", target)
        rule = self.block(MONITORING, "aws_cloudwatch_event_rule", "security_notify")
        self.assertIn("event_pattern = jsonencode({ account = concat(local.security_account_ids, [aws_organizations_account.dolphin.id]) })", rule)
        # The old SNS execution role is kept for one more change (deleting it in the same apply made a cycle), but
        # nothing delivers through it.
        self.assertNotIn("role_arn", target)

    def test_only_the_rule_may_write_to_the_inbox(self):
        policy = self.block(TF, "aws_sqs_queue_policy", "security_alert_digest_inbox")
        self.assertIn('Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.security_notify.arn } }', policy)
        self.assertIn('"aws:SecureTransport" = "false"', policy)

    def test_failures_dead_letter_and_alarm_on_the_same_topic(self):
        inbox = self.block(TF, "aws_sqs_queue", "security_alert_digest_inbox")
        self.assertIn("deadLetterTargetArn = aws_sqs_queue.security_alert_digest_dlq.arn", inbox)
        self.assertIn("message_retention_seconds = 1209600", inbox)
        mapping = self.block(TF, "aws_lambda_event_source_mapping", "security_alert_digest")
        self.assertIn('function_response_types            = ["ReportBatchItemFailures"]', mapping)
        for alarm in ("security_alert_digest_errors", "security_alert_digest_dlq", "security_alert_digest_not_running",
                      "security_alert_digest_inbox_age"):
            self.assertIn("alarm_actions       = [aws_sns_topic.cost_alerts.arn]", self.block(TF, "aws_cloudwatch_metric_alarm", alarm))
        self.assertIn('treat_missing_data  = "breaching"', self.block(TF, "aws_cloudwatch_metric_alarm", "security_alert_digest_not_running"))

    def test_the_function_can_reach_only_its_own_table_queue_topic_and_logs(self):
        policy = self.block(TF, "aws_iam_role_policy", "security_alert_digest")
        resources = re.findall(r"Resource = (.+)", policy)
        self.assertEqual(resources, ["aws_dynamodb_table.security_alert_digest.arn", "aws_sqs_queue.security_alert_digest_inbox.arn",
                                     "aws_sns_topic.cost_alerts.arn", '"*"', '"${aws_cloudwatch_log_group.security_alert_digest.arn}:*"'])
        # The one "*" is the read-only backup-copy check (#577): exactly these three actions, nothing that writes.
        star = policy[policy.index('Sid      = "CheckBackupCopyShares"'):]
        star = star[:star.index("}")]
        self.assertIn('Action   = ["ec2:DescribeSnapshots", "ec2:DescribeSnapshotAttribute", "cloudtrail:LookupEvents"]', star)
        self.assertEqual(policy.count('"*"'), 1)
        function = self.block(TF, "aws_lambda_function", "security_alert_digest")
        self.assertIn('architectures    = ["arm64"]', function)
        self.assertIn('runtime          = "python3.12"', function)
        self.assertIn('content  = file("${path.module}/scripts/security-alert-digest.py")', TF)

    def test_the_flush_schedule_dead_letters_into_the_watched_central_queue(self):
        target = self.block(TF, "aws_cloudwatch_event_target", "security_alert_digest_flush")
        self.assertIn("dead_letter_config { arn = aws_sqs_queue.security_delivery_failures.arn }", target)
        dlq_policy = self.block(MONITORING, "aws_sqs_queue_policy", "security_delivery_failures")
        self.assertIn("aws_cloudwatch_event_rule.security_alert_digest_flush.arn", dlq_policy)
        self.assertIn('schedule_expression = "rate(1 minute)"', self.block(TF, "aws_cloudwatch_event_rule", "security_alert_digest_flush"))

    def test_the_observer_may_read_the_pipeline(self):
        policy = self.block(MONITORING, "aws_iam_role_policy", "security_delivery_health")
        self.assertIn("aws_sqs_queue.security_alert_digest_dlq.arn", policy)
        self.assertIn("aws_cloudwatch_event_rule.security_alert_digest_flush.arn", policy)
        self.assertIn('Action = "lambda:GetFunctionConfiguration", Resource = aws_lambda_function.security_alert_digest.arn', policy)


if __name__ == "__main__":
    unittest.main(verbosity=1)

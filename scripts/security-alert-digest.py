"""security-alert-digest: turn the security alert stream into a few readable emails.

security-alert-digest.tf has the why and the wiring. In short: rule security-alerts-notify used to publish every
matching event straight to the cost-alerts topic, one email each. Inspector and Security Hub re-send every open
finding on each rescan or re-import, so on 2026-10-18/19 that was 3,608 emails in a day and SNS disabled email on the
topic for abuse. Now the rule delivers into an SQS queue and this function decides what is mailed.

Two entry points, one function:
  * an SQS batch (the queue's event source mapping): record each alert, then try to send;
  * the one-minute schedule (no "Records"): only try to send.

Recording. Every alert has a stable identity (Security Hub finding Id, GuardDuty finding id, Inspector findingArn,
Access Analyzer finding id, CloudTrail eventID). One DynamoDB transaction marks the identity seen and adds a pending
item, on the condition that it was not seen in the last REALERT_AFTER seconds. If the condition fails, the alert is an
update of something already mailed: only its last-seen time moves, nothing is mailed. Updates keep a live finding's
marker fresh, so a finding re-imported every day is mailed once; one that comes back after a week of silence (it was
resolved or suppressed, then reopened) is mailed again. Markers expire after SEEN_TTL.

Sending. At most one message per SEND_INTERVAL, whatever comes in, enforced by a lease on one control item (a
conditional update), so concurrent invocations cannot both send. A message carries every pending immediate alert
(GuardDuty, CloudTrail audit events, Access Analyzer) and, once per DIGEST_WINDOW, the pending digest alerts (Security
Hub controls, Inspector CVEs, anything else), summarised by source, severity, control or CVE and resource, with the
first TOP_N in full and "N more". Pending items are deleted only after SNS accepted the message, so a failed publish
loses nothing: it is retried at the next minute and the Lambda error alarm fires. A failure between publish and delete
can repeat items in the next message; it never drops one.

Why DynamoDB pending items plus a schedule rather than holding the digest in SQS: the identity check and the pending
write must be one atomic step (a transaction), and a digest must be read as a whole to count and rank it. SQS stays
where it is good, as the durable inbox with retries and a dead-letter queue in front of all of this.
"""
import collections
import datetime
import hashlib
import json
import os
import time
import urllib.parse

import boto3

TABLE = os.environ.get("STATE_TABLE", "security-alert-digest")
TOPIC = os.environ.get("SNS_TOPIC_ARN", "")
ACCOUNT_NAMES = json.loads(os.environ.get("ACCOUNT_NAMES") or "{}")

SEND_INTERVAL = 60            # the hard rate cap: one message a minute at most
DIGEST_WINDOW = 600           # at most one digest every ten minutes
DIGEST_QUIET = 120            # a digest goes out once no new digest item has arrived for two minutes...
DIGEST_MAX_WAIT = 600         # ...or its oldest item has waited ten
REALERT_AFTER = 7 * 86400     # silent this long, then seen again: mailed again
SEEN_TTL = 30 * 86400         # seen markers expire (DynamoDB TTL on "expires")
LEASE = 300                   # longer than the function timeout
MAX_ITEMS = 3000              # pending items read per kind per run; the rest go in the next message
TOP_N = 25
FIELD_LIMIT = 300

IMMEDIATE, DIGEST = "i#", "d#"
CONTROL = {"pk": {"S": "control"}, "sk": {"S": "send"}}

_ddb = _sns = None


def ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb")
    return _ddb


def sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def now():
    return int(time.time())


def N(value):
    return {"N": str(int(value))}


def S(value):
    return {"S": str(value)}


def text(value, limit=FIELD_LIMIT):
    """One line, bounded: field values come from AWS events and may be long or multi-line."""
    if value is None:
        return ""
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[:limit - 3] + "..."


def error_code(exc):
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")


def identity_key(identity):
    identity = str(identity)
    return identity if len(identity) <= 512 else "sha256:" + hashlib.sha256(identity.encode()).hexdigest()


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def guardduty_label(severity):
    if severity >= 9:
        return "CRITICAL"
    if severity >= 7:
        return "HIGH"
    if severity >= 4:
        return "MEDIUM"
    return "LOW"


def console(region, path):
    region = region if region and all(c.isalnum() or c == "-" for c in region) else "us-west-1"
    return "https://" + region + ".console.aws.amazon.com/" + path.replace("{region}", region)


def alert(identity, kind, source, title, account, region, severity="", resource="", group="", detail="", link=""):
    return {"identity": identity_key(identity), "kind": kind, "source": source, "title": text(title) or "(no title)",
            "account": text(account, 20), "region": text(region, 30), "severity": text(severity, 20),
            "resource": text(resource), "group": text(group, 120), "detail": text(detail), "link": text(link, 600)}


def guardduty_resource(resource):
    if not isinstance(resource, dict):
        return ""
    kind = resource.get("resourceType") or ""
    instance = (resource.get("instanceDetails") or {}).get("instanceId")
    key = resource.get("accessKeyDetails") or {}
    buckets = resource.get("s3BucketDetails") or [{}]
    name = instance or key.get("userName") or key.get("principalId") or (buckets[0] or {}).get("name") \
        or (resource.get("eksClusterDetails") or {}).get("name") or ""
    return (kind + " " + name).strip()


def native_copy(finding, event_account):
    """A Security Hub copy of a finding that already alerts through its own branch (the pattern's ProductArn exclusion):
    every GuardDuty finding (member accounts' arrive natively here too, as their administrator), and Inspector findings of
    this account. A member account's Inspector finding (the Dolphin account's) reaches here only as this copy, through
    the dolphin-security-inspector rule, so it is kept."""
    product = str(finding.get("ProductArn") or "")
    return product.endswith(":product/aws/guardduty") or \
        (product.endswith(":product/aws/inspector") and finding.get("AwsAccountId") == event_account)


def securityhub_matches(finding, event_account=None):
    """The central rule matches an event if ANY finding in it matches, so re-check each one."""
    return (isinstance(finding, dict) and finding.get("Id")
            and not native_copy(finding, event_account)
            and (finding.get("Severity") or {}).get("Label") in ("HIGH", "CRITICAL")
            and finding.get("RecordState") == "ACTIVE"
            and (finding.get("Workflow") or {}).get("Status") == "NEW")


def cloudtrail_title(detail):
    name = detail.get("eventName") or "?"
    who = (detail.get("userIdentity") or {}).get("type")
    failed = ((detail.get("responseElements") or {}).get("ConsoleLogin") == "Failure")
    if name == "ConsoleLogin":
        return ("Root" if who == "Root" else "Console") + " sign-in" + (" FAILED" if failed else "")
    title = name + " (" + str(detail.get("eventSource") or "?") + ")"
    if who == "Root":
        title = "Root user: " + title
    if detail.get("errorCode"):
        title += " denied: " + str(detail["errorCode"])
    return title


CLOUDTRAIL_TARGETS = ("roleName", "userName", "groupName", "policyArn", "groupId", "keyId", "backupVaultName",
                      "backupPlanId", "name", "trailName", "detectorId", "analyzerName")


def alerts_from_event(event):
    """The alerts in one EventBridge event: identity, immediate or digest, and the fields an email shows."""
    if not isinstance(event, dict):
        raise ValueError("not an EventBridge event")
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    source, kind_name = event.get("source"), event.get("detail-type")
    account, region = str(event.get("account") or ""), str(event.get("region") or "")

    if source == "aws.guardduty" and kind_name == "GuardDuty Finding":
        severity = number(detail.get("severity"))
        acct, reg = detail.get("accountId") or account, detail.get("region") or region
        finding_id = detail.get("id") or event.get("id")
        return [alert("guardduty:%s:%s:%s" % (acct, reg, finding_id), IMMEDIATE if severity >= 4 else DIGEST,
                      "GuardDuty", detail.get("title") or detail.get("type"), acct, reg,
                      "%s %g" % (guardduty_label(severity), severity), guardduty_resource(detail.get("resource")),
                      detail.get("type"), detail.get("description"),
                      console(reg, "guardduty/home?region={region}#/findings?fId=" + urllib.parse.quote(str(finding_id))))]

    if source == "aws.access-analyzer" and kind_name == "Access Analyzer Finding":
        acct, reg = detail.get("accountId") or account, detail.get("region") or region
        public = detail.get("isPublic") is True
        return [alert("access-analyzer:%s:%s:%s" % (acct, reg, detail.get("id") or event.get("id")), IMMEDIATE,
                      "Access Analyzer", ("Public" if public else "External") + " access to " + str(detail.get("resourceType") or "a resource"),
                      acct, reg, "public" if public else "", detail.get("resource"), detail.get("resourceType"),
                      "principal " + json.dumps(detail.get("principal"), sort_keys=True) if detail.get("principal") else "",
                      console(reg, "access-analyzer/home?region={region}#/findings"))]

    if kind_name in ("AWS API Call via CloudTrail", "AWS Console Sign In via CloudTrail"):
        acct = detail.get("recipientAccountId") or account
        reg = detail.get("awsRegion") or region
        params = detail.get("requestParameters") if isinstance(detail.get("requestParameters"), dict) else {}
        target = next((str(params[key]) for key in CLOUDTRAIL_TARGETS if params.get(key)), "")
        actor = (detail.get("userIdentity") or {}).get("arn") or (detail.get("userIdentity") or {}).get("type") or ""
        event_id = detail.get("eventID") or event.get("id")
        return [alert("cloudtrail:%s:%s" % (acct, event_id), IMMEDIATE, "CloudTrail", cloudtrail_title(detail), acct, reg,
                      "", target, detail.get("eventName"),
                      "by " + str(actor) + (" from " + str(detail.get("sourceIPAddress")) if detail.get("sourceIPAddress") else ""),
                      console(reg, "cloudtrailv2/home?region={region}#/events/" + urllib.parse.quote(str(event_id))))]

    if source == "aws.securityhub" and kind_name == "Security Hub Findings - Imported":
        found = []
        for finding in detail.get("findings") or []:
            if not securityhub_matches(finding, event.get("account")):
                continue
            reg = finding.get("Region") or region
            inspector = str(finding.get("ProductArn") or "").endswith(":product/aws/inspector")
            resources = finding.get("Resources") or [{}]
            vulns = finding.get("Vulnerabilities") or [{}]
            group = ((finding.get("Compliance") or {}).get("SecurityControlId") or (vulns[0] or {}).get("Id")
                     or finding.get("GeneratorId") or finding.get("ProductName"))
            found.append(alert("securityhub:" + str(finding["Id"]), DIGEST, "Inspector" if inspector else "Security Hub",
                               finding.get("Title"), finding.get("AwsAccountId") or account, reg,
                               (finding.get("Severity") or {}).get("Label"), (resources[0] or {}).get("Id"), group, "",
                               console(reg, "securityhub/home?region={region}#/findings")))
        return found

    if source == "aws.inspector2" and kind_name == "Inspector2 Finding":
        arn = detail.get("findingArn") or event.get("id")
        reg = (str(arn).split(":")[3] if str(arn).startswith("arn:") and str(arn).count(":") >= 5 else "") or region
        resources = detail.get("resources") or [{}]
        vuln = (detail.get("packageVulnerabilityDetails") or {}).get("vulnerabilityId")
        return [alert("inspector:" + str(arn), DIGEST, "Inspector", detail.get("title"),
                      detail.get("awsAccountId") or account, reg, detail.get("severity"),
                      (resources[0] or {}).get("id"), vuln or detail.get("type"), "",
                      console(reg, "inspector/v2/home?region={region}#/findings/all"))]

    # Anything else on the bus: not mailed one by one, but never dropped either.
    return [alert("event:%s:%s" % (account, event.get("id") or hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()),
                  DIGEST, text(source, 60) or "EventBridge", kind_name or "Unrecognised security event", account, region)]


def record_alert(item, t):
    """Mark the identity seen and queue it, atomically, unless it was seen recently. True if it was new."""
    seen_key = {"pk": S("seen#" + item["identity"]), "sk": S("-")}
    values = {":now": N(t), ":exp": N(t + SEEN_TTL)}
    try:
        ddb().transact_write_items(TransactItems=[
            {"Update": {"TableName": TABLE, "Key": seen_key,
                        "UpdateExpression": "SET last_seen = :now, expires = :exp, first_seen = if_not_exists(first_seen, :now)",
                        "ConditionExpression": "attribute_not_exists(last_seen) OR last_seen < :stale",
                        "ExpressionAttributeValues": dict(values, **{":stale": N(t - REALERT_AFTER)})}},
            {"Put": {"TableName": TABLE, "Item": {
                "pk": S("pending"), "sk": S(item["kind"] + item["identity"]),
                "received": N(t), "alert": S(json.dumps(item, sort_keys=True))}}},
        ])
        return True
    except Exception as exc:
        reasons = (getattr(exc, "response", None) or {}).get("CancellationReasons") or []
        if error_code(exc) != "TransactionCanceledException" or not reasons or (reasons[0] or {}).get("Code") != "ConditionalCheckFailed":
            raise
    # Already mailed: an update of a live finding. Keep its marker fresh, mail nothing.
    ddb().update_item(TableName=TABLE, Key=seen_key, UpdateExpression="SET last_seen = :now, expires = :exp",
                      ExpressionAttributeValues=values)
    return False


def pending(prefix):
    """Pending items of one kind, oldest identity order, at most MAX_ITEMS. Returns (items, more_left)."""
    items, start = [], None
    while True:
        args = {"TableName": TABLE, "ConsistentRead": True,
                "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
                "ExpressionAttributeValues": {":pk": S("pending"), ":prefix": S(prefix)},
                "Limit": min(1000, MAX_ITEMS - len(items))}
        if start:
            args["ExclusiveStartKey"] = start
        response = ddb().query(**args)
        for raw in response.get("Items", []):
            try:
                body = json.loads(raw["alert"]["S"])
            except Exception:
                body = alert(raw["sk"]["S"], prefix, "unknown", "(unreadable pending item)", "", "")
            items.append({"key": {"pk": raw["pk"], "sk": raw["sk"]}, "received": int(number(raw.get("received", {}).get("N"))),
                          "alert": body})
        start = response.get("LastEvaluatedKey")
        if not start:
            return items, False
        if len(items) >= MAX_ITEMS:
            return items, True


def delete(items):
    for index in range(0, len(items), 25):
        requests = {TABLE: [{"DeleteRequest": {"Key": item["key"]}} for item in items[index:index + 25]]}
        for attempt in range(5):
            unprocessed = ddb().batch_write_item(RequestItems=requests).get("UnprocessedItems") or {}
            if not unprocessed.get(TABLE):
                break
            requests = unprocessed
            time.sleep(0.2 * (attempt + 1))
        else:
            raise RuntimeError("pending items not deleted after a successful publish; they will be sent again")


def account_label(account):
    name = ACCOUNT_NAMES.get(account)
    return account + (" (" + name + ")" if name else "") if account else "?"


def block(item):
    lines = ["[" + item["source"] + "] " + item["title"]]
    for label, value in (("Severity", item.get("severity")), ("Account", account_label(item.get("account", ""))),
                         ("Region", item.get("region")), ("Resource", item.get("resource")),
                         ("Type", item.get("group")), ("Detail", item.get("detail")), ("Console", item.get("link"))):
        if value:
            lines.append("  " + label + ": " + value)
    return "\n".join(lines)


def one_line(item):
    severity = ("[" + item["severity"] + "] ") if item.get("severity") else ""
    return ("  - " + severity + item["source"] + ": " + item["title"] + " -- " + (item.get("resource") or "?")
            + " (" + account_label(item.get("account", "")) + ", " + (item.get("region") or "?") + ")")


def counts(title, values, limit=10):
    counter = collections.Counter(value or "(none)" for value in values)
    lines = [title]
    for value, count in counter.most_common(limit):
        lines.append("  %6d  %s" % (count, value))
    if len(counter) > limit:
        lines.append("  ... and %d more" % (len(counter) - limit))
    return lines


def subject_line(value):
    """SNS email subjects: ASCII, no control characters, under 100 characters."""
    value = "".join(c if 32 <= ord(c) < 127 else "?" for c in " ".join(value.split()))
    return value if len(value) < 100 else value[:96] + "..."


def stamp(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


FOOTER = ("--\nsecurity-alert-digest (ejc3/aws security-alert-digest.tf): each finding is mailed once when first seen, "
          "and again only after 7 days without updates. GuardDuty, CloudTrail audit events and Access Analyzer go out at "
          "once; Security Hub controls and Inspector CVEs in a digest at most every 10 minutes; never more than one "
          "message a minute.")


def compose(immediate, digest, more_immediate, more_digest, t):
    alerts_i = [item["alert"] for item in immediate]
    alerts_d = [item["alert"] for item in digest]
    if len(alerts_i) == 1 and not alerts_d and not more_immediate:
        only = alerts_i[0]
        return subject_line("Security alert: " + only["source"] + ": " + only["title"]), block(only) + "\n\n" + FOOTER
    parts = []
    if alerts_i:
        parts.append("%d%s immediate" % (len(alerts_i), "+" if more_immediate else ""))
    if alerts_d:
        parts.append("%d%s in digest" % (len(alerts_d), "+" if more_digest else ""))
    lines = ["Security alerts, " + stamp(t), ""]
    if alerts_i:
        lines.append("IMMEDIATE: %d new alert%s" % (len(alerts_i), "" if len(alerts_i) == 1 else "s"))
        lines.extend(counts("By source:", [a["source"] for a in alerts_i]))
        lines.append("")
        for index, item in enumerate(alerts_i[:TOP_N], 1):
            lines.append("%d. %s" % (index, block(item)))
        rest = len(alerts_i) - TOP_N
        if rest > 0 or more_immediate:
            lines.append("... and %s more immediate alerts%s." % (max(rest, 0), " (more still queued: next message)" if more_immediate else ""))
        lines.append("")
    if alerts_d:
        oldest = min(item["received"] for item in digest)
        lines.append("DIGEST: %d new finding%s since %s" % (len(alerts_d), "" if len(alerts_d) == 1 else "s", stamp(oldest)))
        lines.extend(counts("By source:", [a["source"] for a in alerts_d]))
        lines.extend(counts("By severity:", [a.get("severity") for a in alerts_d]))
        lines.extend(counts("By control or CVE:", [a.get("group") for a in alerts_d]))
        lines.extend(counts("By resource:", [a.get("resource") for a in alerts_d]))
        lines.extend(counts("By account:", [account_label(a.get("account", "")) for a in alerts_d]))
        lines.append("First %d:" % min(TOP_N, len(alerts_d)))
        rank = {"CRITICAL": 0, "HIGH": 1}
        ordered = sorted(alerts_d, key=lambda a: (rank.get(str(a.get("severity", "")).split(" ")[0], 2), a["source"], a["title"]))
        lines.extend(one_line(item) for item in ordered[:TOP_N])
        rest = len(alerts_d) - TOP_N
        if rest > 0 or more_digest:
            lines.append("  ... and %d more%s." % (max(rest, 0), " (more still queued: next digest)" if more_digest else ""))
        links = sorted({a["link"] for a in alerts_d if a.get("link")})[:5]
        if links:
            lines.append("Consoles:")
            lines.extend("  " + link for link in links)
        lines.append("")
    total = len(alerts_i) + len(alerts_d)
    lines.append("Total in this message: %d." % total)
    body = "\n".join(lines) + "\n\n" + FOOTER
    if len(body.encode()) > 240_000:  # SNS's limit is 256 KB
        body = body.encode()[:240_000].decode(errors="ignore") + "\n[truncated]"
    return subject_line("Security alerts: " + ", ".join(parts)), body


def release(sent_at=None, digest_sent=False):
    expression, values = "SET lease_until = :zero", {":zero": N(0)}
    if sent_at is not None:
        expression += ", next_send = :next_send"
        values[":next_send"] = N(sent_at + SEND_INTERVAL)
    if digest_sent:
        expression += ", next_digest = :next_digest"
        values[":next_digest"] = N(sent_at + DIGEST_WINDOW)
    ddb().update_item(TableName=TABLE, Key=CONTROL, UpdateExpression=expression, ExpressionAttributeValues=values)


def flush(t):
    """Send at most one message, if the rate cap allows and something is due."""
    try:
        control = ddb().update_item(
            TableName=TABLE, Key=CONTROL, UpdateExpression="SET lease_until = :lease",
            ConditionExpression="(attribute_not_exists(lease_until) OR lease_until < :now) AND (attribute_not_exists(next_send) OR next_send <= :now)",
            ExpressionAttributeValues={":lease": N(t + LEASE), ":now": N(t)}, ReturnValues="ALL_NEW")["Attributes"]
    except Exception as exc:
        if error_code(exc) == "ConditionalCheckFailedException":
            return {"sent": 0, "why": "rate limited or another run is sending"}
        raise
    sent_at, digest = None, []
    try:
        immediate, more_immediate = pending(IMMEDIATE)
        more_digest = False
        if t >= int(number(control.get("next_digest", {}).get("N"))):
            digest, more_digest = pending(DIGEST)
            if digest and not (more_digest or t - max(i["received"] for i in digest) >= DIGEST_QUIET
                               or t - min(i["received"] for i in digest) >= DIGEST_MAX_WAIT):
                digest, more_digest = [], False
        if not immediate and not digest:
            return {"sent": 0, "why": "nothing due"}
        subject, body = compose(immediate, digest, more_immediate, more_digest, t)
        sns().publish(TopicArn=TOPIC, Subject=subject, Message=body)
        sent_at = now()
        delete(immediate + digest)
        return {"sent": 1, "immediate": len(immediate), "digest": len(digest)}
    finally:
        release(sent_at, bool(digest) and sent_at is not None)


def lambda_handler(event, context):
    if isinstance(event, dict) and isinstance(event.get("Records"), list):
        failures, new, seen = [], 0, 0
        for record in event["Records"]:
            try:
                for item in alerts_from_event(json.loads(record["body"])):
                    if record_alert(item, now()):
                        new += 1
                    else:
                        seen += 1
            except Exception as exc:
                # Left on the queue: retried, then the dead-letter queue (alarmed). Never silently dropped.
                print(json.dumps({"record_failed": record.get("messageId"), "error": type(exc).__name__}))
                failures.append({"itemIdentifier": record.get("messageId")})
        try:
            result = flush(now())
        except Exception as exc:
            # The alerts are safely pending; the scheduled run retries every minute and fails loudly there.
            result = {"sent": 0, "why": "send failed: " + type(exc).__name__}
        print(json.dumps({"records": len(event["Records"]), "new": new, "updates": seen, "failed": len(failures), "flush": result}))
        return {"batchItemFailures": failures}
    result = flush(now())
    print(json.dumps({"flush": result}))
    return result

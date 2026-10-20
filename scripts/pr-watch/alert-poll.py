#!/usr/bin/env python3
"""The PR watch's alert step (scripts/pr-watch/SKILL.md, step 6): what the security alert topic would have sent since
SINCE, read from the services' own stores (SNS keeps nothing to poll).

    python3 -I alert-poll.py <ejc3/aws checkout> <SINCE, ISO 8601 UTC>

Scopes are every security-region module in security-monitoring.tf: the main account, and staging through
OrganizationAccountAccessRole. Prints one JSON object per result, each with a stable `id`, and nothing secret.
CloudTrail is read from LOOKBACK before SINCE, because an event can reach lookup-events minutes after it happened
(delivery averages about five minutes and is not guaranteed); the watch drops ids it has already triaged.

The scopes are read concurrently (WORKERS threads, each with its own boto3 session: sessions are not thread-safe).
Read one after another, 34 scopes took minutes, nearly all of it CloudTrail: about 60 lookup-events calls per scope,
which AWS throttles to 2 a second per account and region, so regions in parallel do not slow each other.
"""
import concurrent.futures
import datetime as dt
import json
import re
import sys
import threading

import boto3
from botocore.config import Config

LOOKBACK = dt.timedelta(minutes=30)
WORKERS = 12
OUT_LOCK = threading.Lock()  # one result per line, never two interleaved
CFG = Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=10, read_timeout=30)

SINCE = None  # set by main()
POLLED = set()  # account ids read natively; another account's GuardDuty/Inspector copies in Security Hub are kept
FAILED = []  # a service read that failed: main() exits 1 so the watch does not move its checkpoint past the gap


def scopes(root):
    """(account, region) for every security-region module: the alert rules' coverage."""
    tf = open(f"{root}/security-monitoring.tf").read()
    return sorted({(a, r.replace("_", "-")) for a, r in re.findall(r'module "security_(main|staging)_([a-z0-9_]+)"', tf)})

AUDIT = {
    "cloudtrail.amazonaws.com": {"StopLogging", "DeleteTrail", "UpdateTrail", "PutEventSelectors"},
    "guardduty.amazonaws.com": {"DeleteDetector", "UpdateDetector"},
    "config.amazonaws.com": {"StopConfigurationRecorder", "DeleteConfigurationRecorder"},
    "securityhub.amazonaws.com": {"DisableSecurityHub"},
    "access-analyzer.amazonaws.com": {"DeleteAnalyzer"},
    "iam.amazonaws.com": {"CreateUser", "CreateAccessKey", "CreateLoginProfile", "UpdateLoginProfile", "AddUserToGroup",
                          "AttachUserPolicy", "AttachRolePolicy", "AttachGroupPolicy", "PutUserPolicy", "PutRolePolicy",
                          "PutGroupPolicy", "UpdateAssumeRolePolicy", "DeleteRolePermissionsBoundary",
                          "DeleteUserPermissionsBoundary"},
    "kms.amazonaws.com": {"DisableKey", "ScheduleKeyDeletion", "PutKeyPolicy", "DisableKeyRotation"},
    "ec2.amazonaws.com": {"AuthorizeSecurityGroupIngress", "ModifySecurityGroupRules"},
    "backup.amazonaws.com": {"DeleteBackupVault", "DeleteBackupPlan", "DeleteBackupSelection", "UpdateBackupPlan",
                             "PutBackupVaultAccessPolicy", "DeleteBackupVaultAccessPolicy",
                             "PutBackupVaultLockConfiguration", "DeleteBackupVaultLockConfiguration",
                             "UpdateRecoveryPointLifecycle", "UpdateGlobalSettings", "UpdateRegionSettings",
                             "DeleteRecoveryPoint"},
}
# DeleteRecoveryPoint alerts only for these vaults (routine checkpoint cleanup elsewhere does not page the owner).
PROTECTED_VAULTS = {"ejc3-backup", "ejc3-backup-dr", "fcvm-backups", "ejc3-backup-recovery"}


def sessions():
    """(account label, a function that makes a fresh boto3 session for it): one session per thread."""
    main = boto3.Session()
    yield "main", boto3.Session
    org = main.client("organizations", config=CFG)
    staging = [a for a in org.list_accounts()["Accounts"] if a["Name"] in ("dev-staging", "staging") or "staging" in a["Name"]]
    for a in staging:
        c = main.client("sts", config=CFG).assume_role(
            RoleArn=f"arn:aws:iam::{a['Id']}:role/OrganizationAccountAccessRole", RoleSessionName="alert-poll")["Credentials"]
        creds = {"aws_access_key_id": c["AccessKeyId"], "aws_secret_access_key": c["SecretAccessKey"],
                 "aws_session_token": c["SessionToken"]}
        yield "staging", lambda creds=creds: boto3.Session(**creds)


def out(**kw):
    with OUT_LOCK:
        if "error" in kw and "kind" not in kw:
            FAILED.append(kw)
        print(json.dumps(kw, default=str), flush=True)


def poll(acct, sess, region):
    try:
        sh = sess.client("securityhub", region_name=region, config=CFG)
        f = {"SeverityLabel": [{"Value": v, "Comparison": "EQUALS"} for v in ("HIGH", "CRITICAL")],
             "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
             "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
             "UpdatedAt": [{"Start": SINCE.isoformat(), "End": dt.datetime.now(dt.timezone.utc).isoformat()}]}
        for page in sh.get_paginator("get_findings").paginate(Filters=f):
            for x in page["Findings"]:
                # GuardDuty and Inspector are read natively below for the accounts polled here; a member account's
                # (the Dolphin account's) reach this Security Hub only, so its copies are kept.
                if x["ProductArn"].endswith((":product/aws/inspector", ":product/aws/guardduty")) and \
                        x["AwsAccountId"] in POLLED:
                    continue
                out(src="securityhub", id=x["Id"], acct=acct, region=region, account=x["AwsAccountId"],
                    control=x.get("Compliance", {}).get("SecurityControlId") or x.get("GeneratorId"),
                    title=x["Title"], sev=x["Severity"]["Label"], resource=x["Resources"][0]["Id"])
    except Exception as e:  # noqa: BLE001 - a region without the service is reported, not fatal
        if "not subscribed" not in str(e) and "InvalidAccess" not in str(e):
            out(src="securityhub", acct=acct, region=region, error=type(e).__name__)
    try:
        gd = sess.client("guardduty", region_name=region, config=CFG)
        for det in gd.list_detectors()["DetectorIds"]:
            crit = {"Criterion": {"severity": {"Gte": 4}, "service.archived": {"Eq": ["false"]},
                                  "updatedAt": {"Gte": int(SINCE.timestamp() * 1000)}}}
            ids = [i for page in gd.get_paginator("list_findings").paginate(DetectorId=det, FindingCriteria=crit)
                   for i in page["FindingIds"]]
            for k in range(0, len(ids), 50):  # GetFindings takes at most 50 ids
                for x in gd.get_findings(DetectorId=det, FindingIds=ids[k:k + 50])["Findings"]:
                    out(src="guardduty", id=x["Id"], acct=acct, region=region, account=x["AccountId"], type=x["Type"],
                        sev=x["Severity"], title=x["Title"], resource=x["Resource"].get("ResourceType"))
    except Exception as e:  # noqa: BLE001
        out(src="guardduty", acct=acct, region=region, error=type(e).__name__)
    try:
        ins = sess.client("inspector2", region_name=region, config=CFG)
        fc = {"severity": [{"comparison": "EQUALS", "value": v} for v in ("HIGH", "CRITICAL")],
              "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
              "updatedAt": [{"startInclusive": SINCE, "endInclusive": dt.datetime.now(dt.timezone.utc)}]}
        for page in ins.get_paginator("list_findings").paginate(filterCriteria=fc):
            for x in page["findings"]:
                out(src="inspector", id=x["findingArn"], acct=acct, region=region, account=x["awsAccountId"], sev=x["severity"],
                    title=x["title"][:120], resource=x["resources"][0]["id"])
    except Exception as e:  # noqa: BLE001
        if "AccessDenied" not in str(e):
            out(src="inspector", acct=acct, region=region, error=type(e).__name__)
    try:
        aa = sess.client("accessanalyzer", region_name=region, config=CFG)
        for an in aa.list_analyzers()["analyzers"]:
            for page in aa.get_paginator("list_findings_v2").paginate(analyzerArn=an["arn"],
                                                                       filter={"status": {"eq": ["ACTIVE"]}}):
                for x in page["findings"]:
                    if x.get("updatedAt") and x["updatedAt"] >= SINCE:
                        out(src="access-analyzer", id=x.get("id"), acct=acct, region=region, type=x.get("findingType"),
                            resource=x.get("resource"))
    except Exception as e:  # noqa: BLE001
        out(src="access-analyzer", acct=acct, region=region, error=type(e).__name__)
    try:
        ct = sess.client("cloudtrail", region_name=region, config=CFG)
        now = dt.datetime.now(dt.timezone.utc)
        lookups = [("Username", "root")]
        names = set()
        for src, evs in AUDIT.items():
            if src == "iam.amazonaws.com" and region != "us-east-1":
                continue  # IAM is global: its events are logged in us-east-1
            names |= evs
        names.add("ConsoleLogin")  # IAM users' sign-ins are recorded at the regional endpoint they used
        lookups += [("EventName", n) for n in sorted(names)]
        for key, val in lookups:
            for page in ct.get_paginator("lookup_events").paginate(
                    LookupAttributes=[{"AttributeKey": key, "AttributeValue": val}], StartTime=SINCE - LOOKBACK, EndTime=now):
                for ev in page["Events"]:
                    d = json.loads(ev["CloudTrailEvent"])
                    uid = d.get("userIdentity", {})
                    src, name = d.get("eventSource"), d.get("eventName")
                    if uid.get("type") == "Root":
                        hit = "root"
                    elif name == "ConsoleLogin":
                        if (d.get("responseElements") or {}).get("ConsoleLogin") != "Failure":
                            continue
                        hit = "console-login-failure"
                    elif name in AUDIT.get(src, ()):
                        if name == "DeleteRecoveryPoint" and \
                                (d.get("requestParameters") or {}).get("backupVaultName") not in PROTECTED_VAULTS:
                            continue
                        hit = "tamper-or-iam"
                    else:
                        continue
                    out(src="cloudtrail", id=d.get("eventID"), acct=acct, region=region, kind=hit, event=name, service=src,
                        who=uid.get("arn", uid.get("type")), time=d.get("eventTime"), error=d.get("errorCode"))
    except Exception as e:  # noqa: BLE001
        out(src="cloudtrail", acct=acct, region=region, error=type(e).__name__)

def run_all(work, workers=WORKERS):
    """Poll every (account, session factory, region) concurrently. POLLED is complete before the first poll starts, so
    Security Hub's copies of a polled account's findings are dropped in every scope. A scope that raises is a failed read."""
    def one(item):
        acct, make, region = item
        try:
            poll(acct, make(), region)
        except Exception as e:  # noqa: BLE001 - reported, and main() exits 1
            out(src="scope", acct=acct, region=region, error=type(e).__name__)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, work))


def main(argv):
    global SINCE
    root, SINCE = argv[1], dt.datetime.fromisoformat(argv[2].replace("Z", "+00:00"))
    todo = scopes(root)
    work = []
    for acct, make in sessions():
        POLLED.add(make().client("sts", config=CFG).get_caller_identity()["Account"])
        work += [(acct, make, region) for a, region in todo if a == acct]
    run_all(work)
    if FAILED:
        print(f"alert-poll: {len(FAILED)} service reads failed; do not move alerts_checked_at", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""claude-master-cert-renew: keep every claude-master client certificate from ever expiring.

Runs daily (claude-master-cert-renew.tf). For each configured client it reads the certificate's end date over SSM Run Command
(a fixed document, read-only). When fewer than RENEW_BEFORE_DAYS remain it renews: the client box makes a NEW key and request,
the server signs it (only for that client's own name), and the client box verifies the result and swaps it in atomically
(scripts/ssm/claude-master-cert-*.sh). A renewal does not need the old certificate to still be valid, so a box that was stopped
for a month catches up on the next run.

What it never does: invent a client (an account with no certificate is "not enrolled" and is skipped: enrolling stays a
person's decision, scripts/claude-master-enroll.sh), touch a box that is not running, run anything but the four fixed
documents, or restart a service. The key is made on the client and never travels; only the request and the signed certificate do.

Every run publishes ClientCertDaysLeft per client (namespace ClaudeMasterCerts), so an alarm sees a certificate shrinking even
if this function stops. A failure for one client does not stop the others; after all are handled the run raises, so the Lambda
Errors alarm fires, and the alert topic is told.

`aws lambda invoke --payload '{"dry_run": true}'` shows what it would do. `{"renew_before_days": 60}` renews anything with under 60
days (use it to prove a renewal end to end).
"""
import base64
import json
import os
import re
import time

import boto3

CLIENTS = json.loads(os.environ.get("CLIENTS", "[]"))          # [{"name", "instance", "account"}]
SERVER_NAME = os.environ.get("SERVER_NAME", "claude-master-server")
RENEW_BEFORE_DAYS = int(os.environ.get("RENEW_BEFORE_DAYS", "14"))
CERT_DAYS = int(os.environ.get("CERT_DAYS", "30"))
MIN_DAYS_ON_INSTALL = int(os.environ.get("MIN_DAYS_ON_INSTALL", "20"))
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
HOME_REGION = os.environ.get("AWS_REGION", "us-west-1")
NAMESPACE = "ClaudeMasterCerts"
DOC_STATUS, DOC_REQUEST, DOC_SIGN, DOC_INSTALL = (
    "claude-master-cert-status", "claude-master-cert-request", "claude-master-cert-sign", "claude-master-cert-install")
POLL_SECONDS = 3
COMMAND_TIMEOUT = 150

_PEM = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


class StepFailed(Exception):
    pass


def make_clients():
    return {
        "ec2": boto3.client("ec2", region_name=HOME_REGION),
        "ssm": boto3.client("ssm", region_name=HOME_REGION),
        "cw": boto3.client("cloudwatch", region_name=HOME_REGION),
        "sns": boto3.client("sns", region_name=HOME_REGION),
    }


def find_instance(ec2, name):
    """The one RUNNING instance with this Name tag, or None (a stopped box is left alone and caught up on its next run)."""
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [name]}, {"Name": "instance-state-name", "Values": ["running"]}])
    found = [i["InstanceId"] for r in resp.get("Reservations", []) for i in r.get("Instances", [])]
    return found[0] if len(found) == 1 else None


def parse_pairs(stdout):
    out = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_]+", key):
            out[key] = value
    return out


def run(ssm, document, instance_id, parameters, sleep=time.sleep, timeout=COMMAND_TIMEOUT):
    """Send one fixed document to one instance and return (status, stdout). Parameters are strings, as SSM wants lists."""
    sent = ssm.send_command(
        InstanceIds=[instance_id], DocumentName=document,
        Parameters={k: [str(v)] for k, v in parameters.items()}, TimeoutSeconds=max(30, timeout))
    command_id = sent["Command"]["CommandId"]
    waited = 0
    while waited <= timeout:
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception as e:                                       # the invocation is not visible for a moment
            if "InvocationDoesNotExist" in type(e).__name__ or "InvocationDoesNotExist" in str(e):
                continue
            raise
        if inv["Status"] in ("Success", "Failed", "TimedOut", "Cancelled", "Cancelling"):
            return inv["Status"], inv.get("StandardOutputContent", ""), inv.get("StandardErrorContent", "")
    raise StepFailed("%s on %s did not finish in %ss" % (document, instance_id, timeout))


def ok(result, what):
    status, stdout, stderr = result
    if status != "Success":
        detail = (stdout + " " + stderr).strip().replace("\n", " ")[:300]
        raise StepFailed("%s failed (%s): %s" % (what, status, detail))
    return stdout


def days_left(ssm, instance_id, account, now, sleep):
    """(status, days) for the account's certificate: ('missing', None) when it has none."""
    out = parse_pairs(ok(run(ssm, DOC_STATUS, instance_id, {"Account": account}, sleep), "reading the certificate"))
    if out.get("STATUS") == "missing":
        return "missing", None
    if out.get("STATUS") != "ok" or "NOTAFTER" not in out:
        raise StepFailed("could not read the certificate (%s)" % out.get("STATUS"))
    return "ok", (int(out["NOTAFTER"]) - now) / 86400.0


def split_pems(stdout):
    pems = _PEM.findall(stdout)
    if len(pems) != 2:
        raise StepFailed("the server did not return exactly the client and CA certificates")
    return pems[0] + "\n", pems[1] + "\n"


def renew(clients, client, instance_id, server_id, now, sleep):
    ssm = clients["ssm"]
    name, account = client["name"], client["account"]
    csr = parse_pairs(ok(run(ssm, DOC_REQUEST, instance_id, {"Account": account, "Name": name}, sleep), "making the request")).get("CSR")
    if not csr:
        raise StepFailed("the client returned no certificate request")
    signed = ok(run(ssm, DOC_SIGN, server_id, {"Name": name, "Csr": csr, "Days": CERT_DAYS}, sleep), "signing")
    cert, ca = split_pems(signed)
    installed = parse_pairs(ok(run(ssm, DOC_INSTALL, instance_id, {
        "Account": account, "Name": name, "MinDays": MIN_DAYS_ON_INSTALL,
        "Cert": base64.b64encode(cert.encode()).decode(), "Ca": base64.b64encode(ca.encode()).decode()}, sleep), "installing"))
    if installed.get("INSTALL") != "ok":
        raise StepFailed("installing did not report ok")


def publish_metric(cw, name, days):
    cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
        "MetricName": "ClientCertDaysLeft", "Dimensions": [{"Name": "Client", "Value": name}], "Value": float(days), "Unit": "None"}])


def notify(sns, subject, body):
    if SNS_TOPIC_ARN:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=body)


def handle_client(clients, client, server_id, now, dry_run, threshold, sleep):
    name = client["name"]
    instance_id = find_instance(clients["ec2"], client["instance"])
    if instance_id is None:
        return "%s: %s is not running; skipped" % (name, client["instance"])
    state, left = days_left(clients["ssm"], instance_id, client["account"], now, sleep)
    if state == "missing":
        return "%s: not enrolled (no certificate); skipped" % name
    publish_metric(clients["cw"], name, left)
    if left > threshold:
        return "%s: %.1f days left; fine" % (name, left)
    if dry_run:
        return "%s: %.1f days left; WOULD renew" % (name, left)
    if server_id is None:
        raise StepFailed("the claude-master server is not running, so nothing can be signed")
    renew(clients, client, instance_id, server_id, now, sleep)
    state, after = days_left(clients["ssm"], instance_id, client["account"], now, sleep)
    publish_metric(clients["cw"], name, after)
    if state != "ok" or after < MIN_DAYS_ON_INSTALL:
        raise StepFailed("renewed, but the certificate now has %s days left" % (after if after is not None else "no"))
    return "%s: %.1f days left; RENEWED (now %.1f)" % (name, left, after)


def lambda_handler(event, context, clients=None, now=None, sleep=time.sleep):
    event = event or {}
    clients = clients or make_clients()
    now = now if now is not None else time.time()
    dry_run = bool(event.get("dry_run"))
    threshold = float(event.get("renew_before_days", RENEW_BEFORE_DAYS))
    server_id = find_instance(clients["ec2"], SERVER_NAME)
    report, failures = [], []
    for client in CLIENTS:
        try:
            report.append(handle_client(clients, client, server_id, now, dry_run, threshold, sleep))
        except Exception as e:                                       # one client's trouble must not stop the others
            failures.append("%s: %s" % (client["name"], e))
            report.append("%s: FAILED: %s" % (client["name"], e))
    print("\n".join(report))
    if failures:
        notify(clients["sns"], "claude-master certificate renewal FAILED",
               "These clients could not be checked or renewed (their certificates are NOT expired yet unless said so):\n\n"
               + "\n".join(failures) + "\n\nThe job runs again tomorrow. Details: CloudWatch Logs /aws/lambda/claude-master-cert-renew.")
        raise RuntimeError("%d client(s) failed: %s" % (len(failures), "; ".join(failures)))
    return {"dry_run": dry_run, "report": report}

"""Watch the production fcvm fleet from outside it (fcvm-fleet-prod-alarms.tf). Three jobs, by event:

- {"check": "synthetic"}, every 5 minutes: the public page (when PAGE_URL is set) answers 200 with HTML, and the public
  /live WebSocket (LIVE_URL) opens from the page's origin and sends its hello first, naming a Turnstile site key. Puts
  SyntheticOk 1 when every check passed, 0 when one failed.
- {"check": "worker-deploys"}, every 5 minutes: reads the last WINDOW_MINUTES of the Cloudflare account's audit log
  (v2, GET /accounts/<id>/logs/audit) and counts the writes to the prod Worker (WORKER) made by anything but the prod
  release token (RELEASE_TOKEN_NAME). Puts WorkerChangesOutsideRelease, the count. A read that fails raises, so no
  metric is put and the alarm's missing data breaches.
- An EventBridge "AWS API Call via CloudTrail" event: a change to the prod coordinator's ECS service or to a prod launch
  template. Puts ProdChangesOutsideRelease 1 unless the caller is that API's release role (RELEASE_ROLES).

Every metric is in namespace FCVM/Fleet with the dimension Fleet=FLEET. Details of what failed or who changed what go to
this function's log only: the alarms (and so the email) carry the count.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

NAMESPACE = "FCVM/Fleet"
TIMEOUT = 10
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
READ_METHODS = ("GET", "HEAD", "OPTIONS")
MAX_PAGES = 20
USER_AGENT = "fcvm-fleet-prod-watch"


def put_metric(name, value):
    boto3.client("cloudwatch").put_metric_data(Namespace=NAMESPACE, MetricData=[{
        "MetricName": name, "Value": value, "Unit": "Count",
        "Dimensions": [{"Name": "Fleet", "Value": os.environ["FLEET"]}],
    }])


# ------------------------------------------------------------------------------------------------ the synthetic check
def check_page(url):
    """None when the page answers 200 with HTML, else why not."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            kind = response.headers.get("Content-Type", "")
            if response.status != 200 or "text/html" not in kind:
                return f"{url} answered {response.status} {kind}"
    except Exception as e:  # noqa: BLE001 - every failure is the finding
        return f"{url}: {e}"
    return None


def read_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed mid-frame")
        data += chunk
    return data


def first_message(url, origin, connect=None):
    """The first text message a WebSocket at `url` sends after an upgrade from `origin`."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "wss":
        raise ValueError(f"{url} is not a wss:// URL")
    host, port = parts.hostname, parts.port or 443
    if connect is None:
        raw = socket.create_connection((host, port), timeout=TIMEOUT)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    else:
        sock = connect(host, port)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Origin: {origin}\r\nUser-Agent: {USER_AGENT}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk or len(head) > 16384:
                raise ConnectionError("no complete upgrade response")
            head += chunk
        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        rest = head.split(b"\r\n\r\n", 1)[1]
        if lines[0].split(" ")[1:2] != ["101"]:
            raise ConnectionError(f"upgrade refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != accept:
            raise ConnectionError("upgrade answered with the wrong Sec-WebSocket-Accept")

        def take(n):
            nonlocal rest
            got, rest = rest[:n], rest[n:]
            return got + (read_exact(sock, n - len(got)) if len(got) < n else b"")

        b0, b1 = take(2)
        opcode, length = b0 & 0x0F, b1 & 0x7F
        if not b0 & 0x80 or opcode != 1:
            raise ConnectionError(f"first frame is not one whole text message (opcode {opcode})")
        if b1 & 0x80:
            raise ConnectionError("the server masked its frame")
        if length == 126:
            length = int.from_bytes(take(2), "big")
        elif length == 127:
            length = int.from_bytes(take(8), "big")
        if length > 1 << 20:
            raise ConnectionError(f"first message is {length} bytes")
        message = take(length).decode()
        mask = os.urandom(4)
        sock.sendall(bytes([0x88, 0x82]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(b"\x03\xe8")))
        return message
    finally:
        sock.close()


def check_live(url, origin, connect=None):
    """None when /live's first message is a hello that asks for Turnstile, else why not."""
    try:
        hello = json.loads(first_message(url, origin, connect))
    except Exception as e:  # noqa: BLE001
        return f"{url}: {e}"
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        return f"{url}: the first message is not a hello"
    if not hello.get("turnstile"):
        return f"{url}: the hello names no Turnstile site key, so visitors are not checked"
    return None


def synthetic(connect=None):
    failures = []
    if os.environ.get("PAGE_URL"):
        failures.append(check_page(os.environ["PAGE_URL"]))
    failures.append(check_live(os.environ["LIVE_URL"], os.environ["ORIGIN"], connect))
    failures = [f for f in failures if f]
    for f in failures:
        print(json.dumps({"synthetic_failure": f}))
    put_metric("SyntheticOk", 0 if failures else 1)
    return {"ok": not failures, "failures": failures}


# ------------------------------------------------------------------------------------------------ the prod Worker
def touches_worker(entry, worker):
    """Whether an audit log entry is a write to the Worker script `worker` (its code, versions, deployments, settings or
    secrets): a non-read method on a path with workers/scripts/<worker> or workers/services/<worker>. An entry with no
    method that is not a view counts as a write."""
    raw = entry.get("raw") or {}
    action = entry.get("action") or {}
    method = (raw.get("method") or "").upper()
    if method in READ_METHODS or action.get("type") == "view":
        return False
    path = urllib.parse.urlsplit(raw.get("uri") or "").path
    segments = [s for s in path.split("/") if s]
    for i in range(len(segments) - 2):
        if segments[i] == "workers" and segments[i + 1] in ("scripts", "services") and segments[i + 2] == worker:
            return True
    resource = entry.get("resource") or {}
    # No URI to read: fall back to the resource the log names.
    return not path and resource.get("id") == worker and "worker" in (resource.get("product") or "").lower()


def by_release_token(entry, token_name):
    actor = entry.get("actor") or {}
    return actor.get("context") == "api_token" and actor.get("token_name") == token_name


def foreign_worker_changes(entries, worker, token_name):
    return [e for e in entries if touches_worker(e, worker) and not by_release_token(e, token_name)]


def audit_entries(account, token, since, before, opener=urllib.request.urlopen):
    entries, cursor = [], None
    for _ in range(MAX_PAGES):
        query = {"since": since, "before": before, "limit": "1000"}
        if cursor:
            query["cursor"] = cursor
        url = f"https://api.cloudflare.com/client/v4/accounts/{account}/logs/audit?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
        with opener(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read())
        if not body.get("success") or not isinstance(body.get("result"), list):
            raise RuntimeError(f"audit log read failed: {body.get('errors')}")
        entries += body["result"]
        cursor = (body.get("result_info") or {}).get("cursor")
        if not cursor:
            return entries
    raise RuntimeError(f"audit log has more than {MAX_PAGES} pages in the window; not counting a partial read")


def worker_deploys(now=None, opener=urllib.request.urlopen):
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=int(os.environ["WINDOW_MINUTES"]))
    token = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["CF_TOKEN_SECRET"])["SecretString"].strip()
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    entries = audit_entries(os.environ["CF_ACCOUNT_ID"], token, since.strftime(stamp), now.strftime(stamp), opener)
    foreign = foreign_worker_changes(entries, os.environ["WORKER"], os.environ["RELEASE_TOKEN_NAME"])
    for e in foreign:
        actor, raw, action = e.get("actor") or {}, e.get("raw") or {}, e.get("action") or {}
        print(json.dumps({"worker_change_outside_release": {
            "time": action.get("time"), "result": action.get("result"), "method": raw.get("method"), "uri": raw.get("uri"),
            "actor_type": actor.get("type"), "context": actor.get("context"), "token_name": actor.get("token_name"),
            "token_id": actor.get("token_id"), "email": actor.get("email")}}))
    put_metric("WorkerChangesOutsideRelease", len(foreign))
    return {"entries": len(entries), "outside_release": len(foreign)}


# ------------------------------------------------------------------------------------------------ AWS changes
def named_templates(value):
    """Every LaunchTemplateId and LaunchTemplateName anywhere in a CloudTrail requestParameters."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("LaunchTemplateId", "LaunchTemplateName") and isinstance(v, str):
                found.append(v)
            else:
                found += named_templates(v)
    elif isinstance(value, list):
        for v in value:
            found += named_templates(v)
    return found


def is_prod_change(detail, service, template_ids, template_prefix):
    source, params = detail.get("eventSource"), detail.get("requestParameters") or {}
    if source == "ecs.amazonaws.com":
        named = params.get("service") or ""
        return named == service or named.endswith("/" + service)
    if source == "ec2.amazonaws.com":
        names = named_templates(params)
        # A call naming no template it could be matched by is counted: it may be prod's.
        return not names or any(n in template_ids or n.startswith(template_prefix) for n in names)
    return False


def by_release_role(detail, release_roles):
    identity = detail.get("userIdentity") or {}
    issuer = ((identity.get("sessionContext") or {}).get("sessionIssuer") or {}).get("arn")
    return identity.get("type") == "AssumedRole" and issuer in release_roles.get(detail.get("eventSource"), [])


def unreleased_change(event):
    detail = event.get("detail") or {}
    prod = is_prod_change(detail, os.environ["PROD_SERVICE"], json.loads(os.environ["PROD_TEMPLATE_IDS"]),
                          os.environ["PROD_TEMPLATE_PREFIX"])
    if not prod or by_release_role(detail, json.loads(os.environ["RELEASE_ROLES"])):
        return {"outside_release": 0}
    identity = detail.get("userIdentity") or {}
    print(json.dumps({"prod_change_outside_release": {
        "event": detail.get("eventName"), "source": detail.get("eventSource"), "region": detail.get("awsRegion"),
        "time": detail.get("eventTime"), "principal": identity.get("arn"), "error": detail.get("errorCode"),
        "request": detail.get("requestParameters"), "id": detail.get("eventID")}}, default=str))
    put_metric("ProdChangesOutsideRelease", 1)
    return {"outside_release": 1}


def handler(event, context):
    check = event.get("check")
    if check == "synthetic":
        return synthetic()
    if check == "worker-deploys":
        return worker_deploys()
    if event.get("detail-type") == "AWS API Call via CloudTrail":
        return unreleased_change(event)
    raise ValueError(f"unexpected event: {sorted(event)}")

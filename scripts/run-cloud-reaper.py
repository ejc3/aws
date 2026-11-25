"""run-cloud-reaper: delete run.cloud simulators and sandboxes that were left running. See run-cloud-reaper.tf for why.

run.cloud bills per active minute against a small prepaid balance. Agents are told to give every simulator session an
inactivity timeout and a hard timeout and to delete it when done; this catches the ones that do not. Every five minutes
it lists the account's active iOS and Android simulator sessions, its sandboxes and its boxes, and deletes an ACTIVE
simulator session or a RUNNING, NON-PERSISTENT sandbox when any of these holds:

  1. LIMITS. It has no inactivity timeout and no hard timeout, or a timeout over its cap: the inactivity timeout over
     IDLE_MINUTES or the hard timeout over MAX_AGE_MINUTES. Only once it is GRACE_MINUTES old, so the agent that made
     it has time to finish. For a sandbox the inactivity timeout is its idle pause (idle_pause_seconds) and the hard
     timeout its lifetime (timeout_seconds); a sandbox with either at 0 is persistent (below), so for sandboxes this
     rule is only ever "over the cap".
  2. IDLE. Idle for IDLE_MINUTES or more, measured from the API's last-activity time. ONLY SANDBOXES HAVE ONE
     (last_active_at; idleness counts from the later of that and the current run's start, so a sandbox just resumed is
     not idle from before its pause). A simulator session exposes NO last-activity time: inactivityCountdownSeconds is
     the fixed warning window before run.cloud's own auto-close (10 s on every session seen on 2026-11-24), not the
     time left. So the reaper cannot see a simulator's idleness and uses its AGE instead: run.cloud's own inactivity
     timer, which rule 1 requires to be set and at most IDLE_MINUTES, closes an idle simulator, and rule 3 ends one
     that is kept busy or forgotten. Its log line says idle_minutes null.
  3. AGE. Older than MAX_AGE_MINUTES, whatever its timeouts. A sandbox's age is its current run (since it last started
     or resumed), which is how run.cloud's own lifetime timer counts it.

Never deleted:
  * anything tagged keep=true (KEEP_TAG): the escape hatch for a session that must outlive these rules;
  * a persistent sandbox (idle pause 0 or no lifetime limit: the CLI's --persistent and --expose) and the sandbox behind
    a box (a public hostname). They and the boxes are logged on every run, and listed in its summary line;
  * a sandbox that is not running (paused or stopped: compute billing has stopped, and run.cloud destroys a parked
    sandbox after 48 hours). Destroyed ones are not logged at all;
  * a session the API does not describe well enough to judge (no start time; no timeout fields on a simulator; no idle
    pause or lifetime on a running sandbox, so it is not known whether it is persistent). That is an ERROR, not a keep:
    the run fails at the end so the Errors alarm says the reaper has stopped understanding the API. A simulator that is
    past MAX_AGE_MINUTES is still deleted, since that rule needs only its start time.

Each run logs one JSON line per session it saw (id, kind, age_minutes, idle_minutes, decision, reason, action) and a
summary line, and publishes SessionsReaped and ActiveSessions (namespace RunCloud). A delete that fails is retried by
the next run: the invocation fails only after every session has been handled. {"dry_run": true} reports the decisions
and deletes nothing (and publishes nothing).

The API key is read from Secrets Manager on every invocation and never logged: every line this prints, every error it
raises and the result it returns have the key replaced by [redacted], even where the API echoes it back. HTTP is the
standard library's urllib; boto3 (in the Lambda runtime) reads the secret and publishes the metrics.
"""
import collections
import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# The one label that exempts a session or sandbox from every rule: the run.cloud tag keep=true (`tags` on create).
KEEP_TAG = ("keep", "true")
SIMULATOR_KINDS = ("ios", "android")
# Cloudflare in front of api.run.cloud answers Python-urllib's default User-Agent with 403 (measured 2026-11-24).
USER_AGENT = "run-cloud-reaper/1"
REQUEST_TIMEOUT = 15
MAX_PAGES = 20

API_URL = os.environ.get("API_URL", "https://api.run.cloud").rstrip("/")
SECRET_ID = os.environ.get("SECRET_ID", "run-cloud/api-key")
NAMESPACE = os.environ.get("METRIC_NAMESPACE", "RunCloud")

Limits = collections.namedtuple("Limits", "idle max_age grace")  # seconds


def limits_from_env(env=None):
    env = os.environ if env is None else env
    return Limits(idle=int(env.get("IDLE_MINUTES", "10")) * 60,
                  max_age=int(env.get("MAX_AGE_MINUTES", "60")) * 60,
                  grace=int(env.get("GRACE_MINUTES", "5")) * 60)


# ------------------------------------------------------------------ never print the key

_SECRETS = []


def redact(text):
    text = str(text)
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def emit(record):
    """The only way this prints."""
    print(redact(json.dumps(record, sort_keys=True, default=str)))


# ------------------------------------------------------------------ run.cloud API

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(redact(message))
        self.status = status


def urllib_transport(method, url, headers, timeout):
    """(status, body bytes). An HTTP error status is returned, not raised; a network failure raises."""
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _error_text(body):
    """The API's own short error message ({"error": "..."}), never a whole body."""
    try:
        data = json.loads(body or b"{}")
        message = data.get("error") if isinstance(data, dict) else None
        if isinstance(message, dict):
            message = message.get("message")
        return str(message)[:200] if message else ""
    except (ValueError, TypeError):
        return ""


class RunCloud:
    def __init__(self, key, base, transport):
        self.key, self.base, self.transport = key, base, transport

    def call(self, method, path, parse=True):
        headers = {"Authorization": "Bearer " + self.key, "Accept": "application/json", "User-Agent": USER_AGENT}
        status, body = self.transport(method, self.base + path, headers, REQUEST_TIMEOUT)
        if status >= 400:
            raise ApiError(status, "%s %s: HTTP %d %s" % (method, path, status, _error_text(body)))
        if not parse or not body:
            return None
        return json.loads(body)

    def list_simulators(self, kind):
        """Active sessions only: without ?all=1 the API leaves out released ones."""
        data = self.call("GET", "/run-cloud/%s" % kind)
        if not isinstance(data, list):
            raise ApiError(0, "GET /run-cloud/%s did not return a list" % kind)
        return data

    def list_items(self, path):
        """{"items": [...], "nextCursor": ...} pages, followed to the end."""
        items, cursor, seen = [], None, set()
        for _ in range(MAX_PAGES):
            data = self.call("GET", path + ("?" + urllib.parse.urlencode({"cursor": cursor}) if cursor else ""))
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ApiError(0, "GET %s did not return {items: [...]}" % path)
            items.extend(data["items"])
            cursor = data.get("nextCursor")
            if not cursor:
                return items
            if cursor in seen:
                raise ApiError(0, "GET %s repeated a pagination cursor" % path)
            seen.add(cursor)
        raise ApiError(0, "GET %s had more than %d pages" % (path, MAX_PAGES))

    def delete(self, kind, sid):
        """"deleted", or "already-gone" when the API no longer knows it (404)."""
        path = ("/run-cloud/sandboxes/%s" if kind == "sandbox" else "/run-cloud/" + kind + "/%s") % urllib.parse.quote(sid, safe="")
        try:
            self.call("DELETE", path, parse=False)   # a 2xx is the answer; the body (JSON, text or none) is not needed
        except ApiError as exc:
            if exc.status == 404:
                return "already-gone"
            raise
        return "deleted"


# ------------------------------------------------------------------ decisions (pure)

def parse_time(value):
    """Epoch seconds from an ISO 8601 string (or epoch seconds or milliseconds); None when absent or unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    return None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def field(obj, camel, snake):
    """Sandboxes come snake_case on the wire; the SDK adds camelCase. Accept either."""
    return obj[camel] if camel in obj else obj.get(snake)


def has_field(obj, camel, snake):
    return camel in obj or snake in obj


def kept(obj):
    tags = obj.get("tags")
    return isinstance(tags, dict) and str(tags.get(KEEP_TAG[0], "")).strip().lower() == KEEP_TAG[1]


def mins(seconds):
    return int(seconds // 60)


def decide_simulator(s, now, lim):
    """{"decision": keep|delete|error, "reason", "age", "idle"}. idle is always None: simulators report no activity."""
    created = parse_time(s.get("createdAt"))
    age = None if created is None else max(0.0, now - created)
    out = {"age": age, "idle": None}
    status = s.get("status")
    if status != "active":
        return dict(out, decision="keep", reason="not active (status %s)" % status)
    if kept(s):
        return dict(out, decision="keep", reason="tagged %s=%s" % KEEP_TAG)
    if created is None:
        return dict(out, decision="error", reason="no usable createdAt: its age cannot be told")
    if age >= lim.max_age:
        return dict(out, decision="delete", reason="older than %d minutes (age %d min)" % (mins(lim.max_age), mins(age)))
    if not (has_field(s, "inactivityTimeoutSeconds", "inactivity_timeout_seconds") and has_field(s, "expiresAt", "expires_at")):
        return dict(out, decision="error", reason="the API did not say its inactivity and hard timeouts")
    inactivity = number(field(s, "inactivityTimeoutSeconds", "inactivity_timeout_seconds"))
    if inactivity is not None and inactivity <= 0:
        inactivity = None
    expires = parse_time(field(s, "expiresAt", "expires_at"))
    hard = None if expires is None else expires - created
    over = []
    if inactivity is None and hard is None:
        over.append("no inactivity timeout and no hard timeout")
    if inactivity is not None and inactivity > lim.idle:
        over.append("inactivity timeout %d min is over the %d-minute cap" % (mins(inactivity), mins(lim.idle)))
    if hard is not None and hard > lim.max_age + 1:   # +1: expiresAt and createdAt are each rounded to the millisecond
        over.append("hard timeout %d min is over the %d-minute cap" % (mins(hard), mins(lim.max_age)))
    if over and age >= lim.grace:
        return dict(out, decision="delete", reason="; ".join(over))
    if over:
        return dict(out, decision="keep", reason="%s; within its %d-minute grace period" % ("; ".join(over), mins(lim.grace)))
    return dict(out, decision="keep", reason="within limits (simulators report no last activity: run.cloud's own "
                                             "inactivity timer closes it when idle)")


def decide_sandbox(s, now, lim, box_sandboxes):
    """{"decision", "reason", "age", "idle", "persistent"}. age is the current run, from its start or resume."""
    created = parse_time(field(s, "createdAt", "created_at"))
    changed = parse_time(field(s, "stateChangedAt", "state_changed_at"))
    starts = [t for t in (created, changed) if t is not None]
    run_start = max(starts) if starts else None
    age = None if run_start is None else max(0.0, now - run_start)
    last = parse_time(field(s, "lastActiveAt", "last_active_at"))
    idle = None if last is None else max(0.0, now - max(last, run_start if run_start is not None else last))
    out = {"age": age, "idle": idle, "persistent": False}
    state = s.get("state")
    idle_pause = number(field(s, "idlePauseSeconds", "idle_pause_seconds"))
    lifetime = number(field(s, "timeoutSeconds", "timeout_seconds"))

    if s.get("id") in box_sandboxes:
        return dict(out, persistent=True, decision="keep", reason="persistent: it serves a box (a public hostname); never deleted")
    if idle_pause == 0 or lifetime == 0:
        why = " and ".join(w for w, z in (("idle pause 0", idle_pause == 0), ("no lifetime limit", lifetime == 0)) if z)
        return dict(out, persistent=True, decision="keep", reason="persistent (%s); never deleted" % why)
    if kept(s):
        return dict(out, decision="keep", reason="tagged %s=%s" % KEEP_TAG)
    if state != "running":
        return dict(out, decision="keep", reason="not running (state %s): compute billing has stopped" % state)
    if idle_pause is None or lifetime is None:
        return dict(out, decision="error", reason="the API did not say its idle pause and lifetime: whether it is persistent cannot be told")
    if run_start is None:
        return dict(out, decision="error", reason="no usable createdAt or stateChangedAt: its age cannot be told")
    if age >= lim.max_age:
        return dict(out, decision="delete", reason="running for more than %d minutes (%d min)" % (mins(lim.max_age), mins(age)))
    if idle is not None and idle >= lim.idle:
        return dict(out, decision="delete", reason="idle for %d minutes (lastActiveAt), over the %d-minute cap" % (mins(idle), mins(lim.idle)))
    over = []
    if idle_pause > lim.idle:
        over.append("idle pause %d min is over the %d-minute cap" % (mins(idle_pause), mins(lim.idle)))
    if lifetime > lim.max_age:
        over.append("lifetime %d min is over the %d-minute cap" % (mins(lifetime), mins(lim.max_age)))
    if over and age >= lim.grace:
        return dict(out, decision="delete", reason="; ".join(over))
    if over:
        return dict(out, decision="keep", reason="%s; within its %d-minute grace period" % ("; ".join(over), mins(lim.grace)))
    if idle is None:
        return dict(out, decision="keep", reason="within limits (no lastActiveAt: its own idle pause applies)")
    return dict(out, decision="keep", reason="within limits")


# ------------------------------------------------------------------ AWS and the run

def aws_clients(sm, cw):
    import boto3  # in the Lambda runtime; the offline tests pass fakes and never import it
    return sm or boto3.client("secretsmanager"), cw or boto3.client("cloudwatch")


def read_key(sm):
    try:
        payload = sm.get_secret_value(SecretId=SECRET_ID)["SecretString"]
    except Exception as exc:
        raise RuntimeError("could not read %s: %s" % (SECRET_ID, exc)) from None
    try:
        key = json.loads(payload)["RUN_CLOUD_API_KEY"]
    except Exception as exc:
        # The exception's class only: nothing that could quote the payload.
        raise RuntimeError("%s is not JSON with a RUN_CLOUD_API_KEY (%s)" % (SECRET_ID, exc.__class__.__name__)) from None
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("%s holds no RUN_CLOUD_API_KEY" % SECRET_ID)
    return key.strip()


def line(kind, obj, verdict, action, dry_run):
    name = obj.get("displayName") or obj.get("name") or ""
    return {
        "id": obj.get("id"), "kind": kind, "name": str(name)[:80],
        "age_minutes": None if verdict.get("age") is None else round(verdict["age"] / 60.0, 1),
        "idle_minutes": None if verdict.get("idle") is None else round(verdict["idle"] / 60.0, 1),
        "decision": verdict["decision"], "reason": verdict["reason"], "action": action, "dry_run": dry_run,
    }


def lambda_handler(event, context, transport=None, sm=None, cw=None, now=None):
    dry_run = bool((event or {}).get("dry_run"))
    lim = limits_from_env()
    now = time.time() if now is None else now
    if sm is None or cw is None:
        sm, cw = aws_clients(sm, cw)
    key = read_key(sm)
    _SECRETS[:] = [key]
    api = RunCloud(key, API_URL, transport or urllib_transport)

    results, errors, persistent = [], [], []
    reaped = active = 0
    listed_everything = True

    def act(kind, obj, verdict):
        nonlocal reaped
        action = "none"
        if not isinstance(obj.get("id"), str) or not obj["id"]:
            verdict = dict(verdict, decision="error", reason="no id in the API's answer")
        if verdict["decision"] == "delete":
            if dry_run:
                action = "would-delete"
            else:
                try:
                    action = api.delete(kind, str(obj.get("id")))
                    reaped += action == "deleted"
                except Exception as exc:  # one failed delete must not stop the others; the next run retries it
                    action = "delete-failed"
                    errors.append("delete %s %s: %s" % (kind, obj.get("id"), redact(exc)))
        elif verdict["decision"] == "error":
            errors.append("%s %s: %s" % (kind, obj.get("id"), verdict["reason"]))
        record = line(kind, obj, verdict, action, dry_run)
        if verdict.get("persistent"):
            persistent.append("%s %s" % (kind, obj.get("id")))
        emit(record)
        results.append(record)

    def failed_list(what, exc):
        nonlocal listed_everything
        listed_everything = False
        errors.append("list %s: %s" % (what, redact(exc)))
        emit({"kind": what, "decision": "error", "reason": "could not list: %s" % redact(exc), "dry_run": dry_run})

    for kind in SIMULATOR_KINDS:
        try:
            sessions = api.list_simulators(kind)
        except Exception as exc:
            failed_list(kind, exc)
            continue
        for s in sessions:
            if not isinstance(s, dict):
                continue
            verdict = decide_simulator(s, now, lim)
            active += s.get("status") == "active"
            act(kind, s, verdict)

    try:
        boxes = api.list_items("/run-cloud/boxes")
    except Exception as exc:
        boxes = None
        failed_list("box", exc)
    for b in boxes or []:
        if isinstance(b, dict):
            record = line("box", b, {"decision": "keep", "reason": "persistent: a box (public hostname); never deleted"}, "none", dry_run)
            persistent.append("box %s" % b.get("id"))
            emit(record)
            results.append(record)

    if boxes is None:
        # Without the box list it cannot be told which sandboxes serve a public hostname: judge none of them this run.
        emit({"kind": "sandbox", "decision": "keep", "reason": "boxes could not be listed: no sandbox is judged this run", "dry_run": dry_run})
    else:
        box_sandboxes = {b.get("sandboxId") or b.get("sandbox_id") for b in boxes if isinstance(b, dict)}
        try:
            sandboxes = api.list_items("/run-cloud/sandboxes")
        except Exception as exc:
            sandboxes = []
            failed_list("sandbox", exc)
        for s in sandboxes:
            if not isinstance(s, dict) or s.get("state") in ("destroyed", "destroying"):
                continue
            active += s.get("state") == "running"
            act("sandbox", s, decide_sandbox(s, now, lim, box_sandboxes))

    if not dry_run:
        metrics = [{"MetricName": "SessionsReaped", "Value": reaped, "Unit": "Count"}]
        if listed_everything:
            metrics.append({"MetricName": "ActiveSessions", "Value": active, "Unit": "Count"})
        try:
            cw.put_metric_data(Namespace=NAMESPACE, MetricData=metrics)
        except Exception as exc:
            errors.append("put_metric_data: %s" % redact(exc))

    summary = {"summary": True, "dry_run": dry_run, "seen": len(results), "active": active, "reaped": reaped,
               "would_delete": sum(r["action"] == "would-delete" for r in results), "errors": len(errors),
               "persistent": persistent,
               "limits_minutes": {"idle": mins(lim.idle), "max_age": mins(lim.max_age), "grace": mins(lim.grace)}}
    emit(summary)
    if errors and not dry_run:
        # Everything has been handled; NOW fail, so AWS/Lambda Errors counts it and the alarm can fire.
        raise RuntimeError(redact("%d problem(s): %s" % (len(errors), "; ".join(errors))))
    return json.loads(redact(json.dumps({"dry_run": dry_run, "results": results, "errors": errors, "summary": summary})))

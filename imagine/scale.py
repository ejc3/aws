"""imagine-scale: the only thing that changes how many tasks the imagine backend runs.

The imagine backend (ECS service `imagine`, imagine.tf) runs zero tasks while nobody has a
document open. Neither the web app nor a task can change the service's desired count; they
can only invoke this function, which sets it to exactly one of two values: 0 or AWAKE_COUNT.

    {"action": "wake"}    From the web app (Vercel, role imagine-waker), each time a
                          signed-in user opens a document. Asleep: start AWAKE_COUNT tasks
                          on the newest `live` image. Awake: record that it is still
                          wanted. Answers {"state": "waking" | "awake", ...}.

    {"action": "sweep"}   From EventBridge Scheduler, every minute. Asks the backend how
                          many sockets are open (STATUS_URL). Sets the count to 0 when none
                          has been open for IDLE_SEC and no wake came within IDLE_SEC, or
                          when the backend has not answered for GRACE_SEC.

    {"action": "deploy"}  From the image build (GitHub Actions, role imagine-deploy) after
                          it has moved the `live` tag. Awake: roll onto the new image.
                          Asleep: nothing to do, the next wake resolves `live` again.

Reserved concurrency 1 (imagine.tf) runs these one at a time, so a wake is never undone by
a sweep that read the service before it.

STATE is two tags on the ECS service, written only when they change:
    imagine:wanted-at          unix time of the last wake (rewritten at most every TOUCH_SEC)
    imagine:unreachable-since  unix time the backend first failed to answer, "0" when it
                               answers
A missing or unreadable tag reads as 0: never wanted, or not unreachable.

WHY "UNREACHABLE" IS TIMED AND NOT COUNTED FROM THE START. A cold start takes about a
minute, and a healthy backend can miss one status request (a rolling deploy, a slow
answer). Only GRACE_SEC of continuous silence means it is broken; then it is stopped
rather than left running for nobody, and the stop is reported, because idleness never
reports itself.

No AWS SDK beyond boto3 (bundled in the Lambda runtime). Tested offline by
scripts/test-imagine-scale.py against a fake ECS client.
"""

import json
import os
import time
import urllib.error
import urllib.request

CLUSTER = os.environ.get("CLUSTER", "imagine")
SERVICE = os.environ.get("SERVICE", "imagine")
AWAKE_COUNT = int(os.environ.get("AWAKE_COUNT", "2"))
IDLE_SEC = int(os.environ.get("IDLE_SEC", "300"))
GRACE_SEC = int(os.environ.get("GRACE_SEC", "600"))
TOUCH_SEC = int(os.environ.get("TOUCH_SEC", "60"))
STATUS_URL = os.environ.get("STATUS_URL", "")
STATUS_TIMEOUT_SEC = float(os.environ.get("STATUS_TIMEOUT_SEC", "3"))
SNS_TOPIC = os.environ.get("SNS_TOPIC_ARN", "")

TAG_WANTED = "imagine:wanted-at"
TAG_UNREACHABLE = "imagine:unreachable-since"

# The status document is a few hundred bytes; never read more than this of an answer.
_MAX_STATUS_BYTES = 16 * 1024

_clients = {}


def _client(name):
    # Lazily created so the offline test can install fakes before the first call.
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


def _unix(value):
    """A tag value as unix seconds; 0 for anything that is not a non-negative integer."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return seconds if seconds >= 0 else 0


def describe():
    """The service as this function sees it. Raises unless it exists and is ACTIVE."""
    out = _client("ecs").describe_services(cluster=CLUSTER, services=[SERVICE], include=["TAGS"])
    services = out.get("services") or []
    if out.get("failures") or len(services) != 1 or services[0].get("status") != "ACTIVE":
        raise RuntimeError("service %s/%s is not ACTIVE: %s" % (CLUSTER, SERVICE, out.get("failures") or "missing"))
    service = services[0]
    tags = {t.get("key"): t.get("value") for t in service.get("tags") or []}
    return {
        "arn": service["serviceArn"],
        "desired": int(service["desiredCount"]),
        "running": int(service["runningCount"]),
        "wanted_at": _unix(tags.get(TAG_WANTED)),
        "unreachable_since": _unix(tags.get(TAG_UNREACHABLE)),
    }


def _tag(service, key, seconds):
    _client("ecs").tag_resource(resourceArn=service["arn"], tags=[{"key": key, "value": str(seconds)}])


def fetch_status():
    """{"sockets": n, "idle_ms": m} from the backend, or None if it gave no usable answer."""
    if not STATUS_URL.startswith("https://") and not STATUS_URL.startswith("http://127.0.0.1:"):
        return None
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=STATUS_TIMEOUT_SEC) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read(_MAX_STATUS_BYTES))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(body, dict):
        return None
    sockets, idle_ms = body.get("sockets"), body.get("idle_ms")
    for number in (sockets, idle_ms):
        # bool is an int in Python; a status that says `true` is not a count.
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            return None
    return {"sockets": sockets, "idle_ms": idle_ms}


def wake(now):
    service = describe()
    if service["desired"] == 0:
        # Recorded before the count changes: whatever happens next, the service is known to
        # be wanted from this moment and to have had no chance to be unreachable yet.
        _tag(service, TAG_WANTED, now)
        _tag(service, TAG_UNREACHABLE, 0)
        # forceNewDeployment resolves the `live` tag again, so a wake runs the newest image.
        _client("ecs").update_service(
            cluster=CLUSTER, service=SERVICE, desiredCount=AWAKE_COUNT, forceNewDeployment=True
        )
        return {"state": "waking", "desired": AWAKE_COUNT, "running": 0}

    if now - service["wanted_at"] >= TOUCH_SEC:
        _tag(service, TAG_WANTED, now)
    state = "awake" if service["running"] >= 1 else "waking"
    return {"state": state, "desired": service["desired"], "running": service["running"]}


def _sleep(reason):
    _client("ecs").update_service(cluster=CLUSTER, service=SERVICE, desiredCount=0)
    return {"did": "sleep", "reason": reason}


def _alert(subject, message):
    print("ALERT: %s: %s" % (subject, message))
    if SNS_TOPIC:
        _client("sns").publish(TopicArn=SNS_TOPIC, Subject=subject[:100], Message=message)


def sweep(now):
    service = describe()
    if service["desired"] == 0:
        return {"did": "nothing", "reason": "asleep"}

    status = fetch_status()
    if status is None:
        since = service["unreachable_since"]
        if since == 0:
            _tag(service, TAG_UNREACHABLE, now)
            return {"did": "nothing", "reason": "unreachable", "for_sec": 0}
        if now - since < GRACE_SEC:
            return {"did": "nothing", "reason": "unreachable", "for_sec": now - since}
        result = _sleep("unreachable")
        _alert(
            "imagine backend stopped: not answering",
            "The imagine backend has not answered %s for %d s with %d task(s) wanted and %d running. "
            "imagine-scale set the service's desired count to 0. The next visitor will start it "
            "again; if it still does not answer, look at the /imagine/server log group."
            % (STATUS_URL, now - since, service["desired"], service["running"]),
        )
        return result

    if service["unreachable_since"] != 0:
        _tag(service, TAG_UNREACHABLE, 0)

    idle = (
        status["sockets"] == 0
        and status["idle_ms"] >= IDLE_SEC * 1000
        and now - service["wanted_at"] >= IDLE_SEC
    )
    if idle:
        return _sleep("idle")
    return {"did": "nothing", "reason": "in use", "sockets": status["sockets"], "idle_ms": status["idle_ms"]}


def deploy(now):
    service = describe()
    if service["desired"] == 0:
        return {"did": "nothing", "reason": "asleep"}
    _client("ecs").update_service(cluster=CLUSTER, service=SERVICE, forceNewDeployment=True)
    return {"did": "rolled"}


_ACTIONS = {"wake": wake, "sweep": sweep, "deploy": deploy}


def lambda_handler(event, context):
    action = event.get("action") if isinstance(event, dict) else None
    if not isinstance(action, str) or action not in _ACTIONS:
        raise ValueError("unknown action: %r" % (action,))
    result = _ACTIONS[action](int(time.time()))
    print(json.dumps(dict(result, action=action), sort_keys=True))
    return result

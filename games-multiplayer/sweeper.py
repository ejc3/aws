"""games-mp-sweeper: the AWS-side backstop that stops over-age match engines.

Every match engine is a standalone Fargate task in the `games` cluster that is supposed to
exit on its own (match over, 60 s with nobody connected, or the game's hard cap). This
Lambda is the part that does not trust that: every 5 minutes it stops any engine that has
outlived its cap, so a hung engine, a crash loop that never posts a result, or a launcher
bug costs at most one cap plus one grace period, never a month of Fargate.

What counts as an engine: any task in the cluster that an ECS service did NOT start. The
only service is `mp-router` (group `service:mp-router`); everything else is launched by the
Vercel lobby with RunTask, one task per match. That is deliberately wider than "has a
`match` tag": a launcher bug that forgets the tags must not buy a task immortality.

The lobby tags every RunTask with `game`, `match`, `env` and `hardcap` (seconds). The
limit for a task is `hardcap + GRACE_SEC` when `hardcap` is a sane positive integer, and
DEFAULT_LIMIT_SEC when it is missing or garbage. `hardcap` is clamped to MAX_HARDCAP_SEC so
a compromised or buggy launcher cannot tag a task with a year-long cap.

Age is measured from `createdAt`, which every task has from the moment RunTask accepts it,
so a task stuck in PENDING (image pull loop, no capacity) is aged too.

No AWS SDK beyond boto3 (bundled in the Lambda runtime). Tested offline by
scripts/test-games-mp-sweeper.py against a fake ECS client.
"""

import datetime
import json
import os

CLUSTER = os.environ.get("CLUSTER", "games")
GRACE_SEC = int(os.environ.get("GRACE_SEC", "600"))
DEFAULT_LIMIT_SEC = int(os.environ.get("DEFAULT_LIMIT_SEC", "7200"))
MAX_HARDCAP_SEC = int(os.environ.get("MAX_HARDCAP_SEC", "14400"))
SNS_TOPIC = os.environ.get("SNS_TOPIC_ARN", "")

# Tasks already on their way out. Stopping them again is a no-op at best.
_STOPPING = {"DEACTIVATING", "STOPPING", "DEPROVISIONING", "STOPPED", "DELETED"}

_clients = {}


def _client(name):
    # Lazily created so the offline test can install fakes before the first call.
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


def limit_seconds(tags):
    """The age in seconds after which this task is stopped."""
    raw = tags.get("hardcap")
    try:
        cap = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_LIMIT_SEC
    if cap <= 0:
        return DEFAULT_LIMIT_SEC
    return min(cap, MAX_HARDCAP_SEC) + GRACE_SEC


def is_engine(task):
    return not str(task.get("group", "")).startswith("service:")


def _running_task_arns(ecs):
    # desiredStatus RUNNING covers every task that is not being stopped: PROVISIONING,
    # PENDING, ACTIVATING and RUNNING all have desiredStatus RUNNING.
    arns, token = [], None
    while True:
        kwargs = {"cluster": CLUSTER, "desiredStatus": "RUNNING", "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = ecs.list_tasks(**kwargs)
        arns.extend(page.get("taskArns", []))
        token = page.get("nextToken")
        if not token:
            return arns


def _notify(subject, body):
    if not SNS_TOPIC:
        return
    try:
        _client("sns").publish(TopicArn=SNS_TOPIC, Subject=subject[:100], Message=body)
    except Exception as e:  # noqa: BLE001 - a failed alert must not stop the sweep
        print("sns publish failed: %s" % e)


def sweep(now=None):
    ecs = _client("ecs")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    arns = _running_task_arns(ecs)
    results = []
    for i in range(0, len(arns), 100):
        described = ecs.describe_tasks(cluster=CLUSTER, tasks=arns[i : i + 100], include=["TAGS"])
        for task in described.get("tasks", []):
            arn = task["taskArn"]
            if not is_engine(task) or task.get("lastStatus") in _STOPPING:
                continue
            tags = {t["key"]: t["value"] for t in task.get("tags", []) if "key" in t}
            age = (now - task["createdAt"]).total_seconds()
            limit = limit_seconds(tags)
            entry = {
                "task": arn,
                "match": tags.get("match"),
                "game": tags.get("game"),
                "env": tags.get("env"),
                "age": int(age),
                "limit": limit,
            }
            if age <= limit:
                entry["action"] = "ok"
                results.append(entry)
                continue
            reason = "games-mp-sweeper: age %ds > limit %ds (hardcap=%s)" % (
                age,
                limit,
                tags.get("hardcap", "missing"),
            )
            try:
                ecs.stop_task(cluster=CLUSTER, task=arn, reason=reason[:255])
                entry["action"] = "stopped"
            except Exception as e:  # noqa: BLE001 - keep sweeping the rest
                entry["action"] = "stop_failed"
                entry["error"] = str(e)
                # Silent failure is how a runaway task outlives its backstop: say so.
                _notify(
                    "games-mp-sweeper FAILED to stop an engine",
                    "Task %s (match %s, game %s) is %ds old, past its %ds limit, but StopTask "
                    "failed: %s\nIt keeps costing money until someone stops it."
                    % (arn, tags.get("match"), tags.get("game"), age, limit, e),
                )
            results.append(entry)
    return results


def lambda_handler(event, context):
    results = sweep()
    stopped = [r for r in results if r["action"] != "ok"]
    # One line per run, so CloudWatch Logs Insights can chart engines and stops over time.
    print(json.dumps({"engines": len(results), "stopped": stopped}))
    return {"engines": len(results), "stopped": len(stopped)}

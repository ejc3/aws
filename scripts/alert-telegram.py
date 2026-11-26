"""alert-telegram: the alert topic (cost-alerts), as short readable Telegram messages (alert-telegram.tf).

Subscribed to the cost-alerts topic beside the email subscription. For each message:

  - a CloudWatch alarm entering ALARM: "🔴 <name>: <the first sentence of its description>"; returning to OK:
    "✅ <name> recovered after N min", but only when this function sent that alarm's ALARM and did not mute it.
    INSUFFICIENT_DATA sends nothing. The account and region are shown only when they are not the main ones.
  - flapping: an alarm that went to ALARM more than FLAP_COUNT times in FLAP_WINDOW sends one "🔁 <name> is
    flapping … muted until HH:MM UTC" and nothing more for that alarm (ALARM or OK) until the mute ends. Every 15
    minutes a scheduled run sweeps the mutes that have ended: an alarm that has sat in ALARM for SETTLED since then
    gets "🔴 <name> is still in ALARM after its mute" once; one back in OK is just cleared.
  - order: Lambda's async queue is not FIFO and retries a failed delivery minutes later, so an event whose
    StateChangeTime is not newer than the newest one already seen for that alarm is dropped (a duplicate, or an
    ALARM retried after its OK). The state (when each alarm fired, the newest change, the mute, the ALARM that was
    sent) is one item per alarm in the alert-telegram-state table.
  - the EventBridge "CloudWatch Alarm State Change" events that watchdog-alarms.tf relays to the topic raw are
    read as the same alarms.
  - anything else (budgets, auto-reboot notices): "ℹ️ <subject>" and the start of the body, whitespace collapsed, a
    JSON body reduced to its readable fields. The security digest and the access-audit notices show their subject
    alone.

Tap to see more (owner, 2026-11-26): under that one visible line, a collapsed Telegram "expandable blockquote" holds the
detail: for an alarm its whole description, CloudWatch's reason (the datapoints), the metric, statistic, threshold and
periods, the previous state, the exact time, account and region, with an "Open in console" button; for anything else
the whole body. Account ids there become account names, e-mail addresses and access key ids are masked. Messages use
parse_mode HTML with every text escaped, and a message Telegram refuses as HTML (400) is sent again as the plain
visible line, so a formatting fault never loses an alert.

The bot token and chat id are the JSON secret alerts/telegram (bot_token, chat_id). Until it has a value this does
nothing and succeeds. Nothing it logs carries the token, the chat id or a message: counts and outcomes only. A failed
send raises, so the invocation fails and Lambda retries it; the state is written only after a send.
"""

import datetime
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET_ID = os.environ.get("SECRET_ID", "alerts/telegram")
TABLE = os.environ.get("STATE_TABLE", "alert-telegram-state")
MAIN_ACCOUNT = os.environ.get("MAIN_ACCOUNT", "")
HOME_REGION = os.environ.get("HOME_REGION", "us-west-1")
ACCOUNT_NAMES = json.loads(os.environ.get("ACCOUNT_NAMES", "{}") or "{}")
FLAP_COUNT = 3
FLAP_WINDOW = 6 * 3600
MUTE = 6 * 3600
KEEP = 14 * 86400  # how long an alarm's item outlives its last change (the table's TTL)
SETTLED = 15 * 60  # how long an alarm must have held ALARM after its mute for the sweep to report it
SUBJECT_ONLY = ("Security alert: ", "Security alerts: ", "Access audit: ")
TOKEN_SHAPE = re.compile(r"\d+:[A-Za-z0-9_-]+")
CHAT_SHAPE = re.compile(r"-?\d+")
BODY_CHARS = 600
TEXT_LIMIT = 4000  # Telegram allows 4096 characters after entity parsing (an escaped "&lt;" counts as one)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
ACCOUNT_ID = re.compile(r"(?<![A-Za-z0-9])\d{12}(?![A-Za-z0-9])")  # not inside a hex id or a UUID
REGION_SHAPE = re.compile(r"[a-z]{2}(-[a-z]+)+-\d+")
# The console capture's raw tail (dev-diagnostics.tf) is boot output, which can carry tokens no pattern here knows:
# its collapsed section stops before it.
CONSOLE_TAIL = "\nLast 40 console lines:"
OPERATORS = {"GreaterThanThreshold": ">", "GreaterThanOrEqualToThreshold": ">=", "LessThanThreshold": "<",
             "LessThanOrEqualToThreshold": "<=", "LessThanLowerOrGreaterThanUpperThreshold": "outside the band",
             "LessThanLowerThreshold": "below the band", "GreaterThanUpperThreshold": "above the band"}
API = "https://api.telegram.org"
TIMEOUT = 10
RETRY_CAP = 20

urlopen = urllib.request.urlopen
sleep = time.sleep


def log(**fields):
    print(json.dumps(fields, sort_keys=True), flush=True)


class NotConfigured(Exception):
    pass


def secrets_client():
    import boto3  # lazily: the offline tests run without site-packages
    return boto3.client("secretsmanager")


def state_table():
    import boto3
    return boto3.resource("dynamodb").Table(TABLE)


def cloudwatch(region):
    import boto3
    return boto3.client("cloudwatch", region_name=region)


def credentials(client):
    try:
        raw = client.get_secret_value(SecretId=SECRET_ID)["SecretString"]
    except Exception as error:  # noqa: BLE001 - only the "no value yet" case is treated as not configured
        code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            raise NotConfigured() from None
        raise
    try:
        value = json.loads(raw)
    except ValueError:
        raise NotConfigured() from None
    if not isinstance(value, dict):
        raise NotConfigured()
    # The token goes into the request URL, so a malformed one (a trailing newline from a pasted file) would make
    # urllib raise an error carrying the URL. Only the shapes Telegram issues are accepted.
    token, chat = str(value.get("bot_token") or "").strip(), str(value.get("chat_id") or "").strip()
    if not TOKEN_SHAPE.fullmatch(token) or not CHAT_SHAPE.fullmatch(chat):
        raise NotConfigured()
    return token, chat


def mask(text):
    """Account ids as their names (else "(account)"), e-mail addresses and access key ids masked."""
    text = EMAIL.sub("(email)", str(text))
    text = ACCESS_KEY.sub("(access key)", text)
    return ACCOUNT_ID.sub(lambda m: ACCOUNT_NAMES.get(m.group(), "(account)"), text)


def compose(text, details="", button=None):
    """The sendMessage fields: the visible line as it always was, and under it, when there is more to show, the
    masked details in a collapsed blockquote (HTML, every text escaped), cut to fit Telegram's limit."""
    text = text[:TEXT_LIMIT]
    # Cut before masking as well as after: masking a very long body is slow, and a timeout would lose the alert.
    details = str(details)
    if len(details) > 3 * TEXT_LIMIT:
        details = details[:3 * TEXT_LIMIT].rsplit(None, 1)[0]  # at whitespace, so no token is masked by halves
    details = mask(details).strip()
    fields = {"text": text}
    if details:
        room = TEXT_LIMIT - len(text) - 1
        if room > 20:
            if len(details) > room:
                details = details[:room - 1] + "…"
            fields = {"text": f"{html.escape(text, quote=False)}\n<blockquote expandable>"
                              f"{html.escape(details, quote=False)}</blockquote>", "parse_mode": "HTML"}
    if button:
        label, url = button
        fields["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": url}]]}
    return fields


def send(token, chat, text, details="", button=None):
    """One sendMessage, retried once on 429 (after its retry_after) or a 5xx; a 400 for an HTML message is sent again
    as the plain visible line. Errors never carry the URL: it holds the token."""
    fields = compose(text, details, button)
    try:
        post(token, chat, fields)
    except TelegramRefused:
        if fields.get("parse_mode") != "HTML":
            raise RuntimeError("telegram sendMessage failed: HTTP 400") from None
        log(outcome="html refused, sent plain")
        post(token, chat, {"text": text[:TEXT_LIMIT]})


class TelegramRefused(Exception):
    pass


def post(token, chat, fields):
    body = json.dumps({"chat_id": chat, **fields, "disable_web_page_preview": True}).encode()
    for attempt in (1, 2):
        request = urllib.request.Request(f"{API}/bot{token}/sendMessage", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                response.read()
            return
        except urllib.error.HTTPError as error:
            status = error.code
            wait = 2
            if status == 429:
                try:
                    wait = float(json.loads(error.read() or b"{}").get("parameters", {}).get("retry_after", 2))
                except (ValueError, AttributeError):
                    wait = 2
            if attempt == 1 and (status == 429 or status >= 500):
                sleep(min(max(wait, 0), RETRY_CAP))
                continue
            if status == 400:
                raise TelegramRefused() from None
            raise RuntimeError(f"telegram sendMessage failed: HTTP {status}") from None
        except (urllib.error.URLError, OSError) as error:
            if attempt == 1:
                sleep(2)
                continue
            raise RuntimeError(f"telegram sendMessage failed: {type(error).__name__}") from None
        except Exception as error:  # noqa: BLE001 - anything else may carry the URL, and with it the token
            raise RuntimeError(f"telegram sendMessage failed: {type(error).__name__}") from None


def parse_time(text, default):
    if not text:
        return default
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00").replace("+0000", "+00:00")).timestamp()
    except ValueError:
        return default


def first_sentence(text):
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text)[:300]


def where(alarm):
    """' (account, region)' when the alarm is not the main account's in the home region, else ''."""
    account = str(alarm.get("AWSAccountId") or "")
    arn = str(alarm.get("AlarmArn") or "")
    parts = arn.split(":")
    region = parts[3] if len(parts) > 4 else ""
    shown = []
    if account and MAIN_ACCOUNT and account != MAIN_ACCOUNT:
        shown.append(ACCOUNT_NAMES.get(account, account))
    if region and region != HOME_REGION:
        shown.append(region)
    return f" ({', '.join(shown)})" if shown else ""


def alarm_key(alarm):
    return f"{alarm.get('AWSAccountId', '')}/{alarm.get('AlarmArn') or alarm.get('AlarmName')}"


def utc_clock(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M UTC")


def region_of(alarm):
    parts = str(alarm.get("AlarmArn") or "").split(":")
    region = parts[3] if len(parts) > 4 else ""
    return region if REGION_SHAPE.fullmatch(region) else HOME_REGION


def console_button(alarm):
    name, region = str(alarm.get("AlarmName") or ""), region_of(alarm)
    if not name:
        return None
    return ("Open in console", f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
                               f"#alarmsV2:alarm/{urllib.parse.quote(name, safe='')}")


def number(value):
    try:
        return f"{float(value):.15g}"
    except (TypeError, ValueError):
        return str(value)


def alarm_details(alarm):
    """Everything an alarm message says, one fact per line (shown collapsed under the visible line)."""
    lines = []
    description = " ".join(str(alarm.get("AlarmDescription") or "").split())
    if description:
        lines.append(description)
    if alarm.get("NewStateReason"):
        lines.append(f"Why: {' '.join(str(alarm['NewStateReason']).split())}")
    old, new = alarm.get("OldStateValue"), alarm.get("NewStateValue")
    if old and new:
        lines.append(f"State: {old} → {new}")
    trigger = alarm.get("Trigger") or {}
    if trigger.get("MetricName"):
        dims = ", ".join(f"{d.get('name') or d.get('Name')}={d.get('value') or d.get('Value')}"
                         for d in trigger.get("Dimensions") or [] if isinstance(d, dict))
        lines.append(f"Metric: {trigger.get('Namespace', '')}/{trigger['MetricName']}" + (f" ({dims})" if dims else ""))
        # An EventBridge event carries no threshold, operator or evaluation periods: only what is known is written.
        rule = ["Rule:", str(trigger.get("Statistic") or trigger.get("ExtendedStatistic") or "")]
        if trigger.get("Threshold") is not None:
            op = trigger.get("ComparisonOperator", "")
            rule += [OPERATORS.get(op, op), number(trigger["Threshold"])]
        periods, datapoints = trigger.get("EvaluationPeriods"), trigger.get("DatapointsToAlarm")
        if periods:
            rule.append(f"in {datapoints} of {periods}" if datapoints and datapoints != periods else f"for {periods}")
        if trigger.get("Period"):
            rule.append(f"{'× ' if periods else 'per '}{trigger['Period']}s")
        if len([x for x in rule if x]) > 1:
            lines.append(" ".join(x for x in rule if x))
        if trigger.get("TreatMissingData"):
            lines.append(f"Missing data: {trigger['TreatMissingData']}")
    elif trigger.get("Metrics"):
        parts = [m.get("Expression") or m.get("Label") or m.get("Id") for m in trigger["Metrics"] if isinstance(m, dict)]
        lines.append("Metric math: " + "; ".join(str(p) for p in parts if p))
    if alarm.get("AlarmRule"):
        lines.append(f"Rule: {alarm['AlarmRule']}")
    if alarm.get("StateChangeTime"):
        lines.append(f"Changed: {alarm['StateChangeTime']}")
    account = str(alarm.get("AWSAccountId") or "")
    lines.append(f"Account: {ACCOUNT_NAMES.get(account, account) or '?'} · Region: {region_of(alarm)}")
    return "\n".join(lines)


def handle_alarm(alarm, when, table, token, chat):
    """Returns what was done: 'sent', 'recovered', 'flapping', 'muted', 'stale' or 'silent'."""
    state = alarm.get("NewStateValue")
    if state not in ("ALARM", "OK"):
        return "silent"
    name = str(alarm.get("AlarmName") or "an alarm")
    key = alarm_key(alarm)
    item = table.get_item(Key={"alarm": key}).get("Item") or {"alarm": key}
    if when <= float(item.get("last_change", 0) or 0):
        return "stale"  # a duplicate, or an older change delivered after a newer one
    item["last_change"] = str(when)
    item["name"] = name
    parts = str(alarm.get("AlarmArn") or "").split(":")
    item["region"] = parts[3] if len(parts) > 4 else HOME_REGION
    item["account"] = str(alarm.get("AWSAccountId") or "")
    muted = float(item.get("muted_until", 0) or 0) > when
    if not muted:
        item.pop("muted_until", None)  # an ended mute is handled here, so the sweep does not report it again
    fired = [float(t) for t in item.get("fired", []) if when - float(t) < FLAP_WINDOW]
    if state == "ALARM":
        fired.append(when)
        item["fired"] = [str(t) for t in fired]
        if muted:
            outcome = "muted"
        elif len(fired) > FLAP_COUNT:
            send(token, chat, f"🔁 {name}{where(alarm)} is flapping ({len(fired)} times in "
                              f"{FLAP_WINDOW // 3600}h); muted until {utc_clock(when + MUTE)}",
                 "Fired at: " + ", ".join(utc_clock(t) for t in fired) + "\n" + alarm_details(alarm),
                 console_button(alarm))
            item["muted_until"] = str(when + MUTE)
            item.pop("sent_at", None)
            outcome = "flapping"
        else:
            reason = first_sentence(alarm.get("AlarmDescription")) or name
            send(token, chat, f"🔴 {name}{where(alarm)}: {reason}", alarm_details(alarm), console_button(alarm))
            item["sent_at"] = str(when)
            outcome = "sent"
    elif muted or "sent_at" not in item:
        outcome = "muted" if muted else "silent"
    else:
        minutes = max(0, int((when - float(item["sent_at"])) // 60))
        send(token, chat, f"✅ {name}{where(alarm)} recovered after {minutes} min", alarm_details(alarm),
             console_button(alarm))
        item.pop("sent_at", None)
        outcome = "recovered"
    item["expires"] = int(when + KEEP)
    table.put_item(Item=item)
    return outcome


def scan(table):
    kwargs = {}
    while True:
        page = table.scan(**kwargs)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def sweep(table, token, chat, now):
    """Mutes that have ended: report an alarm that has held ALARM for SETTLED since, once; clear one back in OK.
    An alarm in another account cannot be read from here and is just cleared. One still changing is left for the
    next sweep (its own events are no longer muted)."""
    counts = {}
    for item in scan(table):
        if "muted_until" not in item or float(item["muted_until"]) > now:
            continue
        name, account = item.get("name"), item.get("account", "")
        outcome = "cleared"
        if name and (not MAIN_ACCOUNT or account in ("", MAIN_ACCOUNT)):
            found = cloudwatch(item.get("region") or HOME_REGION).describe_alarms(AlarmNames=[name])
            alarms = found.get("MetricAlarms", []) + found.get("CompositeAlarms", [])
            current = alarms[0] if alarms else {}
            since = current.get("StateTransitionedTimestamp") or current.get("StateUpdatedTimestamp")
            since = since.timestamp() if hasattr(since, "timestamp") else parse_time(since, now)
            if current.get("StateValue") == "ALARM":
                if now - since < SETTLED:
                    counts["unsettled"] = counts.get("unsettled", 0) + 1
                    continue
                region = item.get("region") or HOME_REGION
                arn = f"arn:aws:cloudwatch:{region}:{account or MAIN_ACCOUNT}:alarm:{name}"
                send(token, chat, f"🔴 {name} is still in ALARM after its mute",
                     alarm_details({"AlarmName": name, "AlarmArn": arn, "AWSAccountId": account or MAIN_ACCOUNT,
                                    "AlarmDescription": current.get("AlarmDescription"),
                                    "NewStateReason": current.get("StateReason")}),
                     console_button({"AlarmName": name, "AlarmArn": arn}))
                item["sent_at"] = str(now)
                outcome = "still in alarm"
        item.pop("muted_until", None)
        item["expires"] = int(now + KEEP)
        table.put_item(Item=item)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def from_eventbridge(message):
    """An EventBridge "CloudWatch Alarm State Change" event (watchdog-alarms.tf relays them raw), as the alarm
    message CloudWatch itself would publish; None for anything else."""
    if not isinstance(message, dict) or message.get("detail-type") != "CloudWatch Alarm State Change":
        return None
    detail = message.get("detail") or {}
    state = detail.get("state") or {}
    resources = message.get("resources") or []
    name = detail.get("alarmName")
    if not name or not state.get("value"):
        return None
    region, account = message.get("region", ""), str(message.get("account", ""))
    metrics = (detail.get("configuration") or {}).get("metrics") or []
    stat = (metrics[0].get("metricStat") or {}) if len(metrics) == 1 and isinstance(metrics[0], dict) else {}
    metric = stat.get("metric") or {}
    trigger = {"MetricName": metric.get("name"), "Namespace": metric.get("namespace"), "Statistic": stat.get("stat"),
               "Period": stat.get("period"),
               "Dimensions": [{"name": k, "value": v} for k, v in (metric.get("dimensions") or {}).items()]} \
        if metric.get("name") else {}
    return {
        "AlarmName": name,
        "NewStateValue": state.get("value"),
        "NewStateReason": state.get("reason"),
        "OldStateValue": (detail.get("previousState") or {}).get("value"),
        "Trigger": trigger,
        "AlarmDescription": (detail.get("configuration") or {}).get("description"),
        "AWSAccountId": account,
        "AlarmArn": resources[0] if resources else f"arn:aws:cloudwatch:{region}:{account}:alarm:{name}",
        "StateChangeTime": state.get("timestamp") or message.get("time"),
    }


def full_body(body):
    """The whole of a non-alarm message for the collapsed section: a JSON object as one "key: value" line per field."""
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict):
        return "\n".join(f"{k}: {v if isinstance(v, (str, int, float)) else json.dumps(v, sort_keys=True)}"
                         for k, v in value.items())
    if isinstance(value, str):
        return value
    return str(body or "")


def readable(body):
    """The start of a non-alarm message, whitespace collapsed; a JSON object reduced to its readable fields."""
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict):
        for field in ("message", "Message", "summary", "description", "detail-type", "title"):
            if isinstance(value.get(field), str) and value[field].strip():
                body = value[field]
                break
        else:
            body = "; ".join(f"{k}: {v}" for k, v in value.items() if isinstance(v, (str, int, float)) and str(v))
    text = " ".join(str(body or "").split())
    return text if len(text) <= BODY_CHARS else text[:BODY_CHARS - 1] + "…"


def lambda_handler(event, context=None):
    try:
        token, chat = credentials(secrets_client())
    except NotConfigured:
        log(outcome="not configured", records=len(event.get("Records", [])))
        return {"configured": False}
    table = state_table()
    if event.get("source") == "aws.events" and event.get("detail-type") == "Scheduled Event":
        counts = sweep(table, token, chat, time.time())
        log(outcome="swept", **counts)
        return {"configured": True, **counts}
    counts = {}
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        message = sns.get("Message", "")
        when = parse_time(sns.get("Timestamp"), time.time())
        try:
            alarm = json.loads(message)
        except (ValueError, TypeError):
            alarm = None
        alarm = from_eventbridge(alarm) or alarm
        if isinstance(alarm, dict) and "AlarmName" in alarm and "NewStateValue" in alarm:
            outcome = handle_alarm(alarm, parse_time(alarm.get("StateChangeTime"), when), table, token, chat)
        else:
            subject = " ".join(str(sns.get("Subject") or "Alert").split())
            if str(message).lstrip('"').startswith("ACCESS AUDIT:"):
                # CloudTrail's access-audit notice (access-audit.tf) has no Subject: its first sentence only.
                subject, text = "Access audit: " + str(message).lstrip('"')[len("ACCESS AUDIT:"):].split(".")[0].strip(), ""
            else:
                if subject.startswith("console captured:"):
                    # Never the raw console tail, shown or collapsed; a body without the separator sends nothing of it.
                    message = str(message).split(CONSOLE_TAIL)[0] if CONSOLE_TAIL in str(message) else ""
                text = "" if subject.startswith(SUBJECT_ONLY) else readable(message)
            # The whole body goes in the collapsed section unless the visible line already shows all of it (a JSON body
            # is shown as "k: v; k: v" there and one per line here).
            whole = full_body(message)
            more = whole if " ".join(whole.replace("\n", "; ").split()) != text and " ".join(whole.split()) != text else ""
            send(token, chat, f"ℹ️ {subject}" + (f"\n{text}" if text else ""), more)
            outcome = "message"
        counts[outcome] = counts.get(outcome, 0) + 1
    log(outcome="done", **counts)
    return {"configured": True, **counts}

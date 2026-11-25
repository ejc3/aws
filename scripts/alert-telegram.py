"""alert-telegram: the alert topic (cost-alerts), as short readable Telegram messages (alert-telegram.tf).

Subscribed to the cost-alerts topic beside the email subscription. For each message:

  - a CloudWatch alarm entering ALARM: "🔴 <name>: <the first sentence of its description>"; returning to OK:
    "✅ <name> recovered after N min", but only when this function sent that alarm's ALARM and did not mute it.
    INSUFFICIENT_DATA sends nothing. The account and region are shown only when they are not the main ones.
  - flapping: an alarm that went to ALARM more than FLAP_COUNT times in FLAP_WINDOW sends one "🔁 <name> is
    flapping" and nothing more for that alarm (ALARM or OK) until the mute ends. The state (when each alarm last
    fired, the mute, the ALARM that was sent) is one item per alarm in the alert-telegram-state table.
  - anything else (budgets, the security digest, auto-reboot notices): "ℹ️ <subject>" and the start of the body,
    whitespace collapsed, a JSON body reduced to its readable fields.

The bot token and chat id are the JSON secret alerts/telegram (bot_token, chat_id). Until it has a value this does
nothing and succeeds. Messages go out as plain text (no parse_mode), so nothing in them is interpreted. Nothing it
logs carries the token, the chat id or a message: counts and outcomes only. A failed send raises, so the
invocation fails and Lambda retries it; the state is written only after a send.
"""

import datetime
import json
import os
import re
import time
import urllib.error
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
BODY_CHARS = 600
TEXT_LIMIT = 4000  # Telegram allows 4096
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
    token, chat = (value.get("bot_token"), value.get("chat_id")) if isinstance(value, dict) else (None, None)
    if not token or not chat:
        raise NotConfigured()
    return str(token), str(chat)


def send(token, chat, text):
    """One sendMessage, retried once on 429 (after its retry_after) or a 5xx. Errors never carry the URL: it holds
    the token."""
    body = json.dumps({"chat_id": chat, "text": text[:TEXT_LIMIT], "disable_web_page_preview": True}).encode()
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
            raise RuntimeError(f"telegram sendMessage failed: HTTP {status}") from None
        except (urllib.error.URLError, OSError) as error:
            if attempt == 1:
                sleep(2)
                continue
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


def handle_alarm(alarm, when, table, token, chat):
    """Returns what was done: 'sent', 'recovered', 'flapping', 'muted' or 'silent'."""
    state = alarm.get("NewStateValue")
    if state not in ("ALARM", "OK"):
        return "silent"
    name = str(alarm.get("AlarmName") or "an alarm")
    key = alarm_key(alarm)
    item = table.get_item(Key={"alarm": key}).get("Item") or {"alarm": key}
    muted = float(item.get("muted_until", 0) or 0) > when
    fired = [float(t) for t in item.get("fired", []) if when - float(t) < FLAP_WINDOW]
    if state == "ALARM":
        fired.append(when)
        item["fired"] = [str(t) for t in fired]
        if muted:
            outcome = "muted"
        elif len(fired) > FLAP_COUNT:
            send(token, chat, f"🔁 {name}{where(alarm)} is flapping ({len(fired)} times in "
                              f"{FLAP_WINDOW // 3600}h); muted for {MUTE // 3600} hours")
            item["muted_until"] = str(when + MUTE)
            item.pop("sent_at", None)
            outcome = "flapping"
        else:
            reason = first_sentence(alarm.get("AlarmDescription")) or name
            send(token, chat, f"🔴 {name}{where(alarm)}: {reason}")
            item["sent_at"] = str(when)
            outcome = "sent"
    else:
        if muted or "sent_at" not in item:
            return "muted" if muted else "silent"
        minutes = max(0, int((when - float(item["sent_at"])) // 60))
        send(token, chat, f"✅ {name}{where(alarm)} recovered after {minutes} min")
        item.pop("sent_at", None)
        outcome = "recovered"
    item["expires"] = int(when + KEEP)
    table.put_item(Item=item)
    return outcome


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
    counts = {}
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        message = sns.get("Message", "")
        when = parse_time(sns.get("Timestamp"), time.time())
        try:
            alarm = json.loads(message)
        except (ValueError, TypeError):
            alarm = None
        if isinstance(alarm, dict) and "AlarmName" in alarm and "NewStateValue" in alarm:
            outcome = handle_alarm(alarm, parse_time(alarm.get("StateChangeTime"), when), table, token, chat)
        else:
            subject = " ".join(str(sns.get("Subject") or "Alert").split())
            text = readable(message)
            send(token, chat, f"ℹ️ {subject}" + (f"\n{text}" if text else ""))
            outcome = "message"
        counts[outcome] = counts.get(outcome, 0) + 1
    log(outcome="done", **counts)
    return {"configured": True, **counts}

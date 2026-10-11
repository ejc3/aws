#!/usr/bin/env python3
"""Host access audit agent.

Reports opens of registered paths, and changes to them, to CloudWatch Logs,
CloudWatch metrics and the operator SNS topic. Which paths are registered is
private operator data: it arrives in the install bundle and never lives here.

  install     read a bundle (JSON) on stdin, place registered files, start the service
  uninstall   stop the service and remove only the files this agent placed
  run         the service itself
  status      print the service mode and registered path count (no secrets)
  test-alert  send one clearly labelled TEST report through the full path

Sensors: fanotify on Linux, eslogger on macOS, and an atime arm-and-poll
fallback on both. Standard library only; Python 3.9 or newer.
"""
import ctypes
import datetime
import hashlib
import hmac
import json
import os
import pwd
import queue
import re
import secrets
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

# The installed service's on-host name comes from the (private) install bundle, so
# the public repository never carries the deployed name. The default is a low-key,
# clearly-custom name; it never impersonates a real system daemon.
DEFAULT_SERVICE_NAME = "hostmetricsd"
SERVICE_NAME_RE = re.compile(r"[a-z][a-z0-9-]{2,30}")
LAUNCHD_LABEL_RE = re.compile(r"local\.[a-z0-9][a-z0-9.-]{1,60}")
INSTALL_ROOT = "/usr/local/libexec/"
ES_SILENCE_SECONDS = 180
LOG_NAME = [DEFAULT_SERVICE_NAME]


class Layout:
    """Every host path and service name, derived from one private service name."""

    def __init__(self, name, label=None):
        if not SERVICE_NAME_RE.fullmatch(name):
            raise ValueError("invalid service name")
        if label is not None and not LAUNCHD_LABEL_RE.fullmatch(label):
            raise ValueError("invalid launchd label")
        self.name = name
        self.config_dir = "/etc/" + name
        self.config = self.config_dir + "/config.json"
        self.state = self.config_dir + "/state.json"
        self.agent_dir = INSTALL_ROOT + name
        self.agent = self.agent_dir + "/" + name
        self.unit = "/etc/systemd/system/" + name + ".service"
        self.label = label or "local." + name
        # The event helper (eslogger on macOS) runs as its own launchd job writing into a
        # root-only FIFO, so Full Disk Access is granted to /usr/bin/eslogger alone.
        self.helper_label = self.label + ".helper"
        self.helper_plist = "/Library/LaunchDaemons/" + self.helper_label + ".plist"
        self.plist = "/Library/LaunchDaemons/" + self.label + ".plist"
        self.helper_dir = "/var/db/" + name
        self.fifo = self.helper_dir + "/events"
        self.log = "/var/log/" + name + ".log"
        self.helper_log = "/var/log/" + name + "-helper.log"

    @classmethod
    def installed(cls):
        """The layout of this installed copy: name from our own path, label from config."""
        script = os.path.abspath(__file__)
        name = os.path.basename(script) if script.startswith(INSTALL_ROOT) else DEFAULT_SERVICE_NAME
        label = load_json(cls(name).config, {}).get("launchd_label")
        return cls(name, label)

MAX_BUNDLE_BYTES = 1 << 20
MAX_FILE_BYTES = 64 << 10
MAX_QUEUE = 1000
DEDUPE_SECONDS = 60
SNS_WINDOW_SECONDS = 600
SNS_WINDOW_LIMIT = 20
HEARTBEAT_SECONDS = 60
POLL_SECONDS = 5
ESLOGGER_MAX_LINE = 4 << 20
DEFAULT_IGNORE = {
    "darwin": [
        # Spotlight and Time Machine read files on their own schedule.
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/Metadata.framework/Versions/A/Support/mdworker_shared",
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/Metadata.framework/Versions/A/Support/mds",
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/Metadata.framework/Versions/A/Support/mds_stores",
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/Metadata.framework/Versions/A/Support/corespotlightd",
        "/System/Library/CoreServices/backupd.bundle/Contents/Resources/backupd",
        "/System/Library/CoreServices/backupd.bundle/Contents/Resources/backupd-helper",
    ],
    "linux": [],
}
HOST_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TEMPLATE_RE = re.compile(r"\{\{([a-z_]+)(?::([0-9]{1,4}))?\}\}")


def log(message):
    # Terse by design: host logs never name a watched path or say what the service checks.
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + LOG_NAME[0] + ": " + message,
          file=sys.stderr, flush=True)


# --- AWS request signing (Signature Version 4) ------------------------------

def _hmac(key, text):
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def sign_request(method, url, headers, body, credentials, region, service, now):
    """Return headers with SigV4 authorization added. `now` is an aware UTC datetime."""
    parsed = urllib.parse.urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    headers = {name.lower(): str(value).strip() for name, value in headers.items()}
    headers["host"] = parsed.netloc
    headers["x-amz-date"] = amz_date
    if credentials.get("session_token"):
        headers["x-amz-security-token"] = credentials["session_token"]
    query = "&".join(sorted(
        urllib.parse.quote(k, safe="-_.~") + "=" + urllib.parse.quote(v, safe="-_.~")
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)))
    signed = sorted(headers)
    canonical = "\n".join([
        method, urllib.parse.quote(parsed.path or "/", safe="/-_.~"), query,
        "".join(name + ":" + " ".join(headers[name].split()) + "\n" for name in signed),
        ";".join(signed), hashlib.sha256(body).hexdigest()])
    scope = amz_date[:8] + "/" + region + "/" + service + "/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = ("AWS4" + credentials["secret_access_key"]).encode("utf-8")
    for part in (amz_date[:8], region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = ("AWS4-HMAC-SHA256 Credential=" + credentials["access_key_id"] + "/" + scope
                                + ", SignedHeaders=" + ";".join(signed) + ", Signature=" + signature)
    return headers


def http_post(url, headers, body, timeout=10):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(1 << 16)


class AwsClient:
    def __init__(self, credentials, region, post=None, clock=None):
        self.credentials = credentials
        self.region = region
        self.post = post or http_post
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))

    def call(self, service, headers, body):
        url = "https://" + service + "." + self.region + ".amazonaws.com/"
        signed = sign_request("POST", url, headers, body, self.credentials, self.region, service, self.clock())
        status, payload = self.post(url, signed, body)
        if status != 200:
            raise RuntimeError(service + " returned HTTP " + str(status) + ": " + payload[:300].decode("utf-8", "replace"))
        return payload

    def query(self, service, params):
        body = urllib.parse.urlencode(params).encode("utf-8")
        return self.call(service, {"content-type": "application/x-www-form-urlencoded; charset=utf-8"}, body)

    def put_metrics(self, namespace, metrics):
        params = {"Action": "PutMetricData", "Version": "2010-08-01", "Namespace": namespace}
        for index, (name, dimensions) in enumerate(metrics, 1):
            prefix = "MetricData.member." + str(index) + "."
            params[prefix + "MetricName"] = name
            params[prefix + "Value"] = "1"
            params[prefix + "Unit"] = "Count"
            for number, (key, value) in enumerate(sorted(dimensions.items()), 1):
                params[prefix + "Dimensions.member." + str(number) + ".Name"] = key
                params[prefix + "Dimensions.member." + str(number) + ".Value"] = value
        self.query("monitoring", params)

    def put_log(self, group, stream, message):
        body = json.dumps({"logGroupName": group, "logStreamName": stream, "logEvents": [
            {"timestamp": int(time.time() * 1000), "message": message}]}).encode("utf-8")
        self.call("logs", {"content-type": "application/x-amz-json-1.1",
                           "x-amz-target": "Logs_20140328.PutLogEvents"}, body)

    def publish(self, topic, subject, message):
        self.query("sns", {"Action": "Publish", "Version": "2010-03-31", "TopicArn": topic,
                           "Subject": subject, "Message": message})


# --- Reporting ---------------------------------------------------------------

METRIC_FOR_KIND = {"file-read": "FileRead", "tamper": "Tamper", "test": "Test"}


def subject_for(report):
    host = report["host"]
    if report["kind"] == "file-read":
        text = "Access audit: " + host + ": " + os.path.basename(report["path"]) + " opened by " + os.path.basename(report.get("exe") or "unknown")
    elif report["kind"] == "tamper":
        text = "Access audit: " + host + ": " + report["reason"]
    else:
        text = "Access audit: " + host + ": TEST alert"
    text = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)
    return text[:100]


class Reporter:
    """Bounded queue drained by one thread. Never blocks a sensor."""

    def __init__(self, config, client, clock=time.monotonic):
        self.config = config
        self.client = client
        self.clock = clock
        self.queue = queue.Queue(maxsize=MAX_QUEUE)
        self.dropped = 0
        self.recent = {}
        self.sns_times = []
        self.suppressed = 0
        self.lock = threading.Lock()

    def submit(self, report):
        report.setdefault("host", self.config["host"])
        report.setdefault("time", datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        if report["kind"] == "file-read":
            key = (report["path"], report.get("pid"))
            now = self.clock()
            with self.lock:
                if now - self.recent.get(key, -DEDUPE_SECONDS - 1) < DEDUPE_SECONDS:
                    return False
                self.recent[key] = now
                if len(self.recent) > MAX_QUEUE:
                    self.recent = {k: v for k, v in self.recent.items() if now - v < DEDUPE_SECONDS}
        try:
            self.queue.put_nowait(report)
            return True
        except queue.Full:
            with self.lock:
                self.dropped += 1
            return False

    def allow_email(self):
        now = self.clock()
        self.sns_times = [t for t in self.sns_times if now - t < SNS_WINDOW_SECONDS]
        if len(self.sns_times) >= SNS_WINDOW_LIMIT:
            self.suppressed += 1
            return False
        self.sns_times.append(now)
        return True

    def deliver(self, report):
        config = self.config
        if report["kind"] == "heartbeat":
            self.retry(lambda: self.client.put_metrics(config["namespace"], [("Heartbeat", {"Host": config["host"]})]))
            return
        with self.lock:
            if self.dropped:
                report["dropped_before"] = self.dropped
                self.dropped = 0
        if self.suppressed:
            report["emails_suppressed_before"] = self.suppressed
        message = json.dumps(report, sort_keys=True)
        metric = METRIC_FOR_KIND[report["kind"]]
        self.retry(lambda: self.client.put_log(config["log_group"], config["host"], message))
        self.retry(lambda: self.client.put_metrics(config["namespace"], [(metric, {}), (metric, {"Host": config["host"]})]))
        if self.allow_email():
            if "emails_suppressed_before" in report:
                self.suppressed = 0
            self.retry(lambda: self.client.publish(config["topic_arn"], subject_for(report), json.dumps(report, indent=2, sort_keys=True)))

    @staticmethod
    def retry(action, attempts=5, sleep=time.sleep):
        for attempt in range(attempts):
            try:
                action()
                return True
            except Exception as error:  # network or AWS error: keep the agent alive
                log("delivery failed (attempt " + str(attempt + 1) + "): " + str(error)[:300])
                sleep(min(2 ** attempt, 30))
        return False

    def run(self):
        while True:
            report = self.queue.get()
            try:
                self.deliver(report)
            except Exception as error:
                log("report lost: " + str(error)[:300])


# --- Process details ---------------------------------------------------------

def _read_bounded(path, limit):
    with open(path, "rb") as handle:
        return handle.read(limit)


def linux_process(pid):
    info = {"pid": pid}
    try:
        info["exe"] = os.readlink("/proc/%d/exe" % pid)
    except OSError:
        pass
    try:
        info["cmdline"] = _read_bounded("/proc/%d/cmdline" % pid, 4096).replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        pass
    try:
        for line in _read_bounded("/proc/%d/status" % pid, 8192).decode("utf-8", "replace").splitlines():
            if line.startswith("PPid:"):
                info["ppid"] = int(line.split()[1])
            elif line.startswith("Uid:"):
                info["uid"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    if "uid" in info:
        try:
            info["user"] = pwd.getpwuid(info["uid"]).pw_name
        except KeyError:
            pass
    current = pid
    for _ in range(8):
        try:
            environ = _read_bounded("/proc/%d/environ" % current, 65536).split(b"\0")
        except OSError:
            break
        found = [item.split(b"=", 1)[1].decode("utf-8", "replace") for item in environ if item.startswith(b"SSH_CONNECTION=")]
        if found:
            info["ssh_connection"] = found[0][:200]
            break
        try:
            status = _read_bounded("/proc/%d/status" % current, 8192).decode("utf-8", "replace")
            current = int(re.search(r"^PPid:\s+(\d+)", status, re.M).group(1))
        except (OSError, AttributeError, ValueError):
            break
        if current <= 1:
            break
    return info


def darwin_process(pid):
    info = {}
    try:
        output = subprocess.run(["/bin/ps", "-wwE", "-o", "command=", "-p", str(pid)], capture_output=True,
                                timeout=2, check=False).stdout[:65536].decode("utf-8", "replace").strip()
        match = re.search(r"\bSSH_CONNECTION=(\S+ \S+ \S+ \S+)", output)
        if match:
            info["ssh_connection"] = match.group(1)
        command = re.split(r" [A-Za-z_][A-Za-z0-9_]*=", output, 1)[0][:4096]
        if command:
            info["cmdline"] = command
    except (OSError, subprocess.SubprocessError):
        pass
    return info


# --- Sensors -----------------------------------------------------------------

# Permission events hold the opener until the agent answers, so even a reader
# that exits at once (cat) can be identified. The answer is always ALLOW.
FAN_CLASS_CONTENT = 0x4
FAN_CLOEXEC = 0x1
FAN_MARK_ADD = 0x1
FAN_OPEN_PERM = 0x10000
FAN_ALLOW = 0x1
FAN_Q_OVERFLOW = 0x4000
FAN_EVENT = struct.Struct("=IBBHQii")
FAN_RESPONSE = struct.Struct("=iI")


class FanotifySensor:
    name = "fanotify"

    def __init__(self, paths, emit, ignore):
        self.paths = set(paths)
        self.emit = emit
        self.ignore = set(ignore)
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
        flags = os.O_RDONLY | getattr(os, "O_LARGEFILE", 0) | os.O_CLOEXEC
        self.fd = self.libc.fanotify_init(FAN_CLASS_CONTENT | FAN_CLOEXEC, flags)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "fanotify_init failed")
        for path in self.paths:
            self.mark(path)

    def mark(self, path):
        if self.libc.fanotify_mark(self.fd, FAN_MARK_ADD, FAN_OPEN_PERM, -100, path.encode()) != 0:
            error = ctypes.get_errno()
            if error != 2:  # a missing path is reported by the integrity check
                log("fanotify_mark failed for a registered path: errno " + str(error))
            return False
        return True

    def parse(self, data):
        offset, own = 0, os.getpid()
        while offset + FAN_EVENT.size <= len(data):
            length, _vers, _res, _meta, mask, fd, pid = FAN_EVENT.unpack_from(data, offset)
            if length < FAN_EVENT.size:
                break
            offset += length
            if mask & FAN_Q_OVERFLOW:
                self.emit({"kind": "tamper", "reason": "sensor queue overflow", "sensor": self.name})
            if fd < 0:
                continue
            report = None
            try:
                report = self.describe(fd, pid, own)
            except Exception as error:
                log("could not describe an open: " + str(error)[:200])
            finally:
                self.respond(fd)
            if report:
                self.emit(report)

    def respond(self, fd):
        try:
            os.write(self.fd, FAN_RESPONSE.pack(fd, FAN_ALLOW))
        finally:
            os.close(fd)

    def describe(self, fd, pid, own):
        try:
            path = os.readlink("/proc/self/fd/%d" % fd)
        except OSError:
            return None
        if pid == own:
            return None
        if path.endswith(" (deleted)"):
            path = path[:-len(" (deleted)")]
        if path not in self.paths:
            return None
        info = linux_process(pid)
        if info.get("exe") in self.ignore:
            return None
        report = {"kind": "file-read", "path": path, "sensor": self.name}
        report.update(info)
        return report

    def run(self):
        while True:
            self.parse(os.read(self.fd, 64 * 1024))


def eslogger_needles(paths):
    """Map JSON-escaped basenames (eslogger escapes '/', so full paths never match raw) to paths."""
    needles = {}
    for path in paths:
        needles.setdefault(json.dumps(os.path.basename(path), ensure_ascii=False)[1:-1].encode("utf-8"), set()).add(path)
    return needles


class EsloggerScanner:
    """Finds registered-path opens in a stream of eslogger JSON lines without parsing every line."""

    def __init__(self, paths, emit, ignore, describe=darwin_process):
        self.paths = set(paths)
        self.needles = eslogger_needles(paths)
        self.emit = emit
        self.ignore = set(ignore)
        self.describe = describe
        self.buffer = b""

    def feed(self, chunk):
        data = self.buffer + chunk
        end = data.rfind(b"\n")
        if end < 0:
            self.buffer = data if len(data) <= ESLOGGER_MAX_LINE else b""
            return
        complete, self.buffer = data[:end], data[end + 1:]
        if not any(needle in complete for needle in self.needles):
            return
        for line in complete.split(b"\n"):
            if any(needle in line for needle in self.needles):
                self.line(line)

    def line(self, line):
        try:
            event = json.loads(line)
            path = event["event"]["open"]["file"]["path"]
            process = event.get("process") or {}
        except (ValueError, KeyError, TypeError):
            return
        if path not in self.paths:
            return
        exe = ((process.get("executable") or {}).get("path"))
        if exe in self.ignore:
            return
        token = process.get("audit_token") or {}
        if token.get("pid") == os.getpid():
            return
        report = {"kind": "file-read", "path": path, "sensor": "eslogger", "exe": exe,
                  "pid": token.get("pid"), "ppid": process.get("ppid"), "uid": token.get("ruid"),
                  "signing_id": process.get("signing_id"), "tty": (process.get("tty") or {}).get("path")}
        try:
            report["user"] = pwd.getpwuid(report["uid"]).pw_name
        except (KeyError, TypeError):
            pass
        if isinstance(report["pid"], int):
            report.update(self.describe(report["pid"]))
        self.emit({k: v for k, v in report.items() if v is not None})


class EsloggerSensor:
    """Reads eslogger output from the FIFO its launchd job writes. macOS opens files
    constantly, so minutes without any event means eslogger is not running or not permitted."""
    name = "eslogger"

    def __init__(self, paths, emit, ignore, fifo, silence=ES_SILENCE_SECONDS, clock=time.monotonic):
        if not stat.S_ISFIFO(os.stat(fifo).st_mode):
            raise OSError(fifo + " is not a FIFO")
        self.scanner = EsloggerScanner(paths, self.seen, ignore)
        self.emit = emit
        self.fifo = fifo
        self.silence = silence
        self.clock = clock
        self.last_report = {}
        self.last_data = clock()
        self.silent = False

    def seen(self, report):
        self.last_report[report["path"]] = self.clock()
        self.emit(report)

    def reported_recently(self, path):
        return self.clock() - self.last_report.get(path, float("-inf")) < DEDUPE_SECONDS

    def poll_once(self, fd, timeout):
        ready, _, _ = select.select([fd], [], [], timeout)
        try:
            chunk = os.read(fd, 1 << 20) if ready else None
        except BlockingIOError:
            chunk = None
        if chunk:
            self.last_data = self.clock()
            if self.silent:
                self.silent = False
                log("helper resumed")
            self.scanner.feed(chunk)
            return
        if chunk == b"":
            time.sleep(min(timeout, 1))  # no writer: eslogger is restarting or not permitted
        if not self.silent and self.clock() - self.last_data > self.silence:
            self.silent = True
            self.emit({"kind": "tamper", "reason": "sensor silent", "sensor": self.name,
                       "detail": "no eslogger events; give /usr/bin/eslogger Full Disk Access"})

    def run(self):
        fd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        while True:
            self.poll_once(fd, 5)


class MacSensor:
    """eslogger identifies the reader; atime polling still catches a read if eslogger is down."""
    name = "eslogger+atime-poll"

    def __init__(self, paths, emit, ignore, fifo):
        self.eslogger = EsloggerSensor(paths, emit, ignore, fifo)
        self.emit = emit
        self.poll = PollSensor(paths, self.fallback)

    def fallback(self, report):
        time.sleep(2)  # give eslogger's attributed report time to arrive first
        if not self.eslogger.reported_recently(report["path"]):
            self.emit(report)

    def mark(self, path):
        return self.poll.mark(path)

    def run(self):
        threading.Thread(target=self.poll.run, name="poll", daemon=True).start()
        self.eslogger.run()


class PollSensor:
    """Arms each path by putting its atime before its mtime; relatime then updates atime on the next read."""
    name = "atime-poll"

    def __init__(self, paths, emit, ignore=()):
        self.paths = set(paths)
        self.emit = emit
        self.armed = {}
        for path in self.paths:
            self.arm(path)

    def arm(self, path):
        try:
            st = os.stat(path)
            atime = st.st_mtime_ns - 86400 * 10**9
            os.utime(path, ns=(atime, st.st_mtime_ns))
            self.armed[path] = atime
        except OSError:
            self.armed.pop(path, None)

    def mark(self, path):
        self.arm(path)
        return path in self.armed

    def check(self):
        for path in sorted(self.paths):
            try:
                atime = os.stat(path).st_atime_ns
            except OSError:
                continue
            if path in self.armed and atime > self.armed[path]:
                self.emit({"kind": "file-read", "path": path, "sensor": self.name})
                self.arm(path)
            elif path not in self.armed:
                self.arm(path)

    def run(self):
        while True:
            time.sleep(POLL_SECONDS)
            self.check()


# --- Integrity ---------------------------------------------------------------

def fingerprint(path):
    st = os.stat(path)
    return {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


class Integrity:
    def __init__(self, baseline, emit, remark):
        self.baseline = {path: dict(value) for path, value in baseline.items()}
        self.emit = emit
        self.remark = remark
        self.reported = {}

    def check(self):
        for path, expected in sorted(self.baseline.items()):
            try:
                current = fingerprint(path)
            except OSError:
                current = None
            if current is None:
                reason = "registered path missing"
            elif current["ino"] != expected["ino"]:
                reason = "registered path replaced"
            elif current != expected:
                reason = "registered path modified"
            else:
                self.reported.pop(path, None)
                continue
            if self.reported.get(path) != (reason, current):
                self.reported[path] = (reason, current)
                self.emit({"kind": "tamper", "reason": reason, "path": path})
                if current is not None:
                    # A recreated file can reuse the inode number; re-marking is idempotent.
                    self.remark(path)


# --- Install -----------------------------------------------------------------

def render(template, values):
    def replace(match):
        name, size = match.group(1), match.group(2)
        if name == "random_hex":
            return secrets.token_hex(int(size or "32"))
        if name not in values:
            raise ValueError("unknown template value {{" + name + "}}")
        return values[name]
    return TEMPLATE_RE.sub(replace, template)


def validate_bundle(bundle):
    def need(condition, message):
        if not condition:
            raise ValueError("invalid bundle: " + message)
    need(isinstance(bundle, dict) and bundle.get("version") == 1, "version must be 1")
    need(isinstance(bundle.get("host"), str) and HOST_RE.fullmatch(bundle["host"]), "host")
    for key in ("region", "topic_arn", "log_group", "namespace"):
        need(isinstance(bundle.get(key), str) and bundle[key], key)
    for key in ("reporter", "principal"):
        need(isinstance(bundle.get(key), dict) and all(isinstance(bundle[key].get(k), str) and bundle[key][k]
                                                      for k in ("access_key_id", "secret_access_key")), key)
    files = bundle.get("files")
    need(isinstance(files, list) and 0 < len(files) <= 50, "files")
    seen = set()
    for item in files:
        need(isinstance(item, dict), "file entry")
        path = item.get("path")
        need(isinstance(path, str) and os.path.isabs(path) and os.path.normpath(path) == path and path not in seen, "file path")
        seen.add(path)
        need(isinstance(item.get("content"), str) and len(item["content"]) <= MAX_FILE_BYTES, "file content")
        need(isinstance(item.get("owner"), str), "file owner")
        need(re.fullmatch(r"0[0-7]{3}", item.get("mode", "0600")), "file mode")
        need(isinstance(item.get("watch", True), bool), "file watch")
        need(isinstance(item.get("age_days", 120), int) and 0 <= item.get("age_days", 120) <= 3650, "file age_days")
    need(isinstance(bundle.get("ignore_executables", []), list), "ignore_executables")
    if "service_name" in bundle:
        need(isinstance(bundle["service_name"], str) and SERVICE_NAME_RE.fullmatch(bundle["service_name"]), "service_name")
    if "launchd_label" in bundle:
        need(isinstance(bundle["launchd_label"], str) and LAUNCHD_LABEL_RE.fullmatch(bundle["launchd_label"]), "launchd_label")


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read(MAX_FILE_BYTES + 1)).hexdigest()


def generate_ssh_key():
    directory = tempfile.mkdtemp(prefix="access-audit-")
    try:
        key = os.path.join(directory, "key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "deploy", "-f", key], check=True)
        with open(key) as private, open(key + ".pub") as public:
            return private.read(), public.read().strip()
    finally:
        shutil.rmtree(directory)


def load_json(path, default):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_private(path, data, mode=0o600):
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def place_files(bundle, previous, chown=True, keygen=generate_ssh_key):
    """Place registered files. Never overwrites a file this agent did not place unchanged."""
    placed = previous.get("files", {})
    for item in bundle["files"]:
        path = item["path"]
        if os.path.lexists(path):
            if path not in placed or not os.path.isfile(path) or os.path.islink(path) or sha256_file(path) != placed[path]["sha256"]:
                raise RuntimeError("refusing to overwrite an existing file the agent did not place: " + path)
    values = {"principal_access_key_id": bundle["principal"]["access_key_id"],
              "principal_secret_access_key": bundle["principal"]["secret_access_key"]}
    if any("{{ssh_" in item["content"] for item in bundle["files"]):
        values["ssh_private_key"], values["ssh_public_key"] = keygen()
    files, created_dirs = {}, list(previous.get("dirs", []))
    for item in bundle["files"]:
        path = item["path"]
        owner = pwd.getpwnam(item["owner"])
        missing = []
        parent = os.path.dirname(path)
        while not os.path.isdir(parent):
            missing.append(parent)
            parent = os.path.dirname(parent)
        for directory in reversed(missing):
            os.mkdir(directory, 0o700)
            if chown:
                os.chown(directory, owner.pw_uid, owner.pw_gid)
            created_dirs.append(directory)
        write_private(path, render(item["content"], values), int(item.get("mode", "0600"), 8))
        if chown:
            os.chown(path, owner.pw_uid, owner.pw_gid)
        stamp = time.time() - item.get("age_days", 120) * 86400 - secrets.randbelow(86400)
        os.utime(path, (stamp, stamp))
        files[path] = {"sha256": sha256_file(path), "watch": item.get("watch", True), "fingerprint": fingerprint(path)}
    for path in set(placed) - set(files):
        remove_placed(path, placed[path])
    return {"files": files, "dirs": created_dirs}


def remove_placed(path, record):
    try:
        if sha256_file(path) == record["sha256"]:
            os.unlink(path)
        else:
            log("left a registered path in place because its content changed: " + path)
    except FileNotFoundError:
        pass


def install_service(layout, platform):
    if platform == "linux":
        with open(layout.unit, "w") as unit:
            unit.write("[Unit]\nDescription=Host metrics and integrity agent\nAfter=network-online.target\nWants=network-online.target\n\n"
                       "[Service]\nExecStart=/usr/bin/python3 -I " + layout.agent + " run\nRestart=always\nRestartSec=5\n\n"
                       "[Install]\nWantedBy=multi-user.target\n")
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", layout.name], check=True)
        subprocess.run(["systemctl", "restart", layout.name], check=True)
    else:
        os.makedirs(layout.helper_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(layout.fifo):
            os.mkfifo(layout.fifo, 0o600)
        with open(layout.helper_plist, "w") as plist:
            plist.write(launchd_plist(layout.helper_label, ["/usr/bin/eslogger", "open"], layout.fifo, layout.helper_log))
        with open(layout.plist, "w") as plist:
            plist.write(launchd_plist(layout.label, ["/usr/bin/python3", "-I", layout.agent, "run"], None, layout.log))
        for label, path in ((layout.label, layout.plist), (layout.helper_label, layout.helper_plist)):
            os.chmod(path, 0o644)
            subprocess.run(["launchctl", "bootout", "system/" + label], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", "system", path], check=True)


def launchd_plist(label, arguments, stdout, stderr):
    def string(value):
        return "<string>" + value.replace("&", "&amp;").replace("<", "&lt;") + "</string>"
    output = "<key>StandardOutPath</key>" + string(stdout) + "\n" if stdout else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n'
            "<key>Label</key>" + string(label) + "\n"
            "<key>ProgramArguments</key><array>" + "".join(string(a) for a in arguments) + "</array>\n"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>\n"
            + output + "<key>StandardErrorPath</key>" + string(stderr) + "\n</dict></plist>\n")


def stop_service(layout, platform):
    if platform == "linux":
        subprocess.run(["systemctl", "disable", "--now", layout.name], capture_output=True)
        if os.path.exists(layout.unit):
            os.unlink(layout.unit)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    else:
        for label, path in ((layout.helper_label, layout.helper_plist), (layout.label, layout.plist)):
            subprocess.run(["launchctl", "bootout", "system/" + label], capture_output=True)
            if os.path.exists(path):
                os.unlink(path)
        if os.path.exists(layout.fifo):
            os.unlink(layout.fifo)
        try:
            os.rmdir(layout.helper_dir)
        except OSError:
            pass


def platform_name():
    return "darwin" if sys.platform == "darwin" else "linux"


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("access-audit: run as root")


def command_install():
    require_root()
    raw = sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise SystemExit("access-audit: bundle too large")
    bundle = json.loads(raw)
    validate_bundle(bundle)
    layout = Layout(bundle.get("service_name", DEFAULT_SERVICE_NAME), bundle.get("launchd_label"))
    LOG_NAME[0] = layout.name
    os.makedirs(layout.config_dir, mode=0o700, exist_ok=True)
    os.chmod(layout.config_dir, 0o700)
    previous = load_json(layout.state, {})
    state = place_files(bundle, previous)
    platform = platform_name()
    config = {key: bundle[key] for key in ("host", "region", "topic_arn", "log_group", "namespace", "reporter")}
    config["ignore_executables"] = DEFAULT_IGNORE[platform] + list(bundle.get("ignore_executables", []))
    config["launchd_label"] = layout.label
    write_private(layout.config, json.dumps(config, indent=2, sort_keys=True))
    write_private(layout.state, json.dumps(state, indent=2, sort_keys=True))
    os.makedirs(layout.agent_dir, mode=0o755, exist_ok=True)
    shutil.copyfile(os.path.abspath(__file__), layout.agent)
    os.chmod(layout.agent, 0o700)
    install_service(layout, platform)
    watched = sum(1 for record in state["files"].values() if record["watch"])
    print(layout.name + ": installed on " + bundle["host"] + ": " + str(len(state["files"])) + " files placed, "
          + str(watched) + " watched")


def command_uninstall():
    require_root()
    layout = Layout.installed()
    LOG_NAME[0] = layout.name
    stop_service(layout, platform_name())
    state = load_json(layout.state, {})
    for path, record in state.get("files", {}).items():
        remove_placed(path, record)
    for directory in reversed(state.get("dirs", [])):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    for path in (layout.config, layout.state, layout.agent):
        if os.path.exists(path):
            os.unlink(path)
    for directory in (layout.agent_dir, layout.config_dir):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    print(layout.name + ": uninstalled")


# --- Service -----------------------------------------------------------------

def choose_sensor(platform, paths, emit, ignore, fifo):
    build = (lambda: FanotifySensor(paths, emit, ignore)) if platform == "linux" else (lambda: MacSensor(paths, emit, ignore, fifo))
    try:
        return build(), None
    except Exception as error:
        reason = ("fanotify" if platform == "linux" else "eslogger") + " unavailable: " + str(error)[:200]
        log(reason)
    return PollSensor(paths, emit), reason


def command_run():
    require_root()
    layout = Layout.installed()
    LOG_NAME[0] = layout.name
    config = load_json(layout.config, None)
    state = load_json(layout.state, None)
    if not config or not state:
        raise SystemExit(layout.name + ": not installed")
    client = AwsClient(config["reporter"], config["region"])
    reporter = Reporter(config, client)
    threading.Thread(target=reporter.run, name="reporter", daemon=True).start()
    watched = sorted(os.path.realpath(path) for path, record in state["files"].items() if record["watch"])
    sensor, degraded = choose_sensor(platform_name(), watched, reporter.submit, config["ignore_executables"], layout.fifo)
    if degraded:
        reporter.submit({"kind": "tamper", "reason": "sensor degraded to " + sensor.name, "detail": degraded})
    sensor_thread = threading.Thread(target=sensor.run, name="sensor", daemon=True)
    sensor_thread.start()
    integrity = Integrity({path: record["fingerprint"] for path, record in state["files"].items()},
                          reporter.submit, getattr(sensor, "mark", lambda path: None))
    log("running: " + str(len(watched)) + " watched paths, sensor " + sensor.name)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    while True:
        if not sensor_thread.is_alive():
            # Report, let the queue drain briefly, and exit so the service manager restarts us.
            reporter.submit({"kind": "tamper", "reason": "sensor stopped", "sensor": sensor.name})
            time.sleep(15)
            raise SystemExit("access-audit: sensor stopped")
        reporter.submit({"kind": "heartbeat"})
        integrity.check()
        time.sleep(HEARTBEAT_SECONDS)


def command_status():
    layout = Layout.installed()
    state = load_json(layout.state, {})
    config = load_json(layout.config, {})
    files = state.get("files", {})
    print(json.dumps({"host": config.get("host"), "installed": bool(config),
                      "files": len(files), "watched": sum(1 for r in files.values() if r["watch"])}))


def command_test_alert():
    require_root()
    layout = Layout.installed()
    LOG_NAME[0] = layout.name
    config = load_json(layout.config, None)
    if not config:
        raise SystemExit(layout.name + ": not installed")
    reporter = Reporter(config, AwsClient(config["reporter"], config["region"]))
    reporter.deliver({"kind": "test", "host": config["host"],
                      "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                      "note": "operator-initiated test; no registered path was opened"})
    print(layout.name + ": test alert sent")


def main(argv):
    commands = {"install": command_install, "uninstall": command_uninstall, "run": command_run,
                "status": command_status, "test-alert": command_test_alert}
    if len(argv) != 2 or argv[1] not in commands:
        raise SystemExit("usage: access-audit.py {" + ",".join(commands) + "}")
    commands[argv[1]]()


if __name__ == "__main__":
    main(sys.argv)

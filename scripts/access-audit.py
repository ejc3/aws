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

Event handler: if <config_dir>/event_handler exists, is a regular root-owned file and is not
group- or world-writable, each file-read and tamper report is written to it as JSON on stdin.
A file-read report carries "ancestry": the reader's process chain, reader first, as up to 16
{"pid": int, "start": str} entries captured when the open was seen ("start" is an opaque
process-start token: /proc/<pid>/stat starttime on Linux, ps lstart on macOS), so a consumer
can tell a live process from a reused pid. The handler has 15 seconds; a nonzero exit or more
than 64 KiB of stdout is reported as a failure, and only a JSON object with "kind": "handler"
on its stdout is delivered.
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
import urllib.error
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
        # Optional, installed out of band: an executable run on each trigger event, whose
        # JSON stdout (if any) is delivered as a report. Absent by default; the agent never
        # installs it. It must be root-owned and not group/world-writable to be run.
        self.event_handler = self.config_dir + "/event_handler"
        # Written at the first install; proves a later install that these directories are this agent's.
        self.marker = self.config_dir + "/.install-marker"

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
MAX_INODES_PER_PATH = 8
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


class PermanentError(RuntimeError):
    """A 4xx that retrying cannot fix (denied, missing resource, bad request); throttling is not one."""


def http_post(url, headers, body, timeout=10):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1 << 16)
    except urllib.error.HTTPError as error:
        payload = error.read(1 << 16)
        text = urllib.parse.urlsplit(url).netloc.split(".")[0] + " returned HTTP " + str(error.code) + ": " + payload[:300].decode("utf-8", "replace")
        if 400 <= error.code < 500 and error.code not in (408, 429) and b"Throttl" not in payload:
            raise PermanentError(text)
        raise RuntimeError(text)


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

METRIC_FOR_KIND = {"file-read": "FileRead", "tamper": "Tamper", "test": "Test", "handler": "Handler"}


def subject_for(report):
    host = report["host"]
    if report["kind"] == "file-read":
        text = "Access audit: " + host + ": " + os.path.basename(report["path"]) + " opened by " + os.path.basename(report.get("exe") or "unknown")
    elif report["kind"] == "tamper":
        text = "Access audit: " + host + ": " + report["reason"]
    elif report["kind"] == "handler":
        text = "Access audit: " + host + ": " + str(report.get("summary", "event handler report"))
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
        """Send one report; returns {destination: True/False} (email is None when rate-limited)."""
        config = self.config
        if report["kind"] == "heartbeat":
            return {"metrics": self.retry(lambda: self.client.put_metrics(config["namespace"], [("Heartbeat", {"Host": config["host"]})]))}
        with self.lock:
            if self.dropped:
                report["dropped_before"] = self.dropped
                self.dropped = 0
        if self.suppressed:
            report["emails_suppressed_before"] = self.suppressed
        message = json.dumps(report, sort_keys=True)
        metric = METRIC_FOR_KIND[report["kind"]]
        # The metric drives the alarm, so it goes first: a failing log or email destination must not delay it.
        results = {"metrics": self.retry(lambda: self.client.put_metrics(config["namespace"], [(metric, {}), (metric, {"Host": config["host"]})])),
                   "log": self.retry(lambda: self.client.put_log(config["log_group"], config["host"], message)),
                   "email": None}
        if self.allow_email():
            if "emails_suppressed_before" in report:
                self.suppressed = 0
            results["email"] = self.retry(lambda: self.client.publish(config["topic_arn"], subject_for(report), json.dumps(report, indent=2, sort_keys=True)))
        return results

    @staticmethod
    def retry(action, attempts=5, sleep=time.sleep):
        for attempt in range(attempts):
            try:
                action()
                return True
            except PermanentError as error:  # retrying a denial or a missing resource only delays the next report
                log("delivery failed (not retried): " + str(error)[:300])
                return False
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


ANCESTRY_DEPTH = 16


def linux_ancestry(pid):
    """[{pid, start}] from the reader up through its parents, start = /proc/<pid>/stat field 22."""
    chain = []
    while pid > 1 and len(chain) < ANCESTRY_DEPTH:
        try:
            data = _read_bounded("/proc/%d/stat" % pid, 4096)
            fields = data[data.rindex(b")") + 2:].split()
            parent, start = int(fields[1]), fields[19].decode("ascii")
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            break
        chain.append({"pid": pid, "start": start})
        pid = parent
    return chain


def darwin_ancestry(pid, table=None):
    """The same chain on macOS from one bounded process table read; start is ps lstart."""
    if table is None:
        try:
            table = subprocess.run(["/bin/ps", "-ax", "-o", "pid=,ppid=,lstart="], capture_output=True, timeout=2,
                                   check=False).stdout[:1 << 20].decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return []
    rows = {}
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[2].strip())
    chain = []
    while pid > 1 and pid in rows and len(chain) < ANCESTRY_DEPTH:
        chain.append({"pid": pid, "start": rows[pid][1]})
        pid = rows[pid][0]
    return chain


def process_ancestry(pid, platform):
    return linux_ancestry(pid) if platform == "linux" else darwin_ancestry(pid)


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
        self.inodes = {}
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
        flags = os.O_RDONLY | getattr(os, "O_LARGEFILE", 0) | os.O_CLOEXEC
        self.fd = self.libc.fanotify_init(FAN_CLASS_CONTENT | FAN_CLOEXEC, flags)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "fanotify_init failed")
        # Fail closed: a registered path this sensor cannot watch (a mark limit, ENOSPC) must not leave a healthy
        # heartbeat over an unwatched file. Raising here makes choose_sensor fall back to polling, and command_run
        # reports that degradation as tamper.
        failed = [error for error in (self._add_mark(path) for path in self.paths) if error not in (None, 2)]
        if failed:
            os.close(self.fd)
            raise OSError(failed[0], "fanotify_mark failed for " + str(len(failed)) + " registered path(s)")

    def _add_mark(self, path):
        """None when marked; else the errno (2: the path is missing, which the integrity check reports)."""
        if self.libc.fanotify_mark(self.fd, FAN_MARK_ADD, FAN_OPEN_PERM, -100, path.encode()) != 0:
            return ctypes.get_errno()
        self.remember(path)
        return None

    def mark(self, path):
        error = self._add_mark(path)
        if error is None:
            return True
        if error != 2:
            log("fanotify_mark failed for a registered path: errno " + str(error))
            if self.emit:  # a re-mark that fails leaves the new inode unwatched: say so remotely
                self.emit({"kind": "tamper", "reason": "could not watch a registered path (errno " + str(error) + ")",
                           "sensor": self.name})
        return False

    def remember(self, path):
        # The mark is on the inode, so an open through any other name (a hard link, a bind
        # mount) is reported too: events are matched by inode, then by name. Only watched
        # paths are recorded; the integrity check re-marks unwatched files as well.
        real = os.path.realpath(path)
        if real not in self.paths:
            return
        try:
            st = os.stat(real)
        except OSError:
            return
        # Keep the inodes this path had before: a replaced file keeps its mark, so `mv f f.x; cp f.x f`
        # followed by a read of f.x must still be reported as the registered path. Bounded per path.
        previous = [key for key, value in self.inodes.items() if value == real and key != (st.st_dev, st.st_ino)]
        for key in previous[:-(MAX_INODES_PER_PATH - 1)] if len(previous) >= MAX_INODES_PER_PATH else []:
            del self.inodes[key]
        self.inodes[(st.st_dev, st.st_ino)] = real

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
        st = os.fstat(fd)
        registered = self.inodes.get((st.st_dev, st.st_ino)) or (path if path in self.paths else None)
        if registered is None:
            return None
        info = linux_process(pid)
        if info.get("exe") in self.ignore:
            return None
        report = {"kind": "file-read", "path": registered, "sensor": self.name}
        if path != registered:
            report["opened_path"] = path
        report.update(info)
        # The reader is still held on the permission event here, so its chain is intact.
        ancestry = linux_ancestry(pid)
        if ancestry:
            report["ancestry"] = ancestry
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

    def __init__(self, paths, emit, ignore, describe=darwin_process, ancestry=darwin_ancestry, ignored=None):
        self.paths = set(paths)
        self.needles = eslogger_needles(paths)
        self.emit = emit
        self.ignore = set(ignore)
        self.describe = describe
        self.ancestry = ancestry
        self.ignored = ignored or (lambda path: None)
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
            self.ignored(path)  # its read still moves atime; the poll fallback must not report it
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
            report["ancestry"] = self.ancestry(report["pid"]) or None
        self.emit({k: v for k, v in report.items() if v is not None})


class EsloggerSensor:
    """Reads eslogger output from the FIFO its launchd job writes. macOS opens files
    constantly, so minutes without any event means eslogger is not running or not permitted."""
    name = "eslogger"

    def __init__(self, paths, emit, ignore, fifo, silence=ES_SILENCE_SECONDS, clock=time.monotonic):
        if not stat.S_ISFIFO(os.stat(fifo).st_mode):
            raise OSError(fifo + " is not a FIFO")
        self.scanner = EsloggerScanner(paths, self.seen, ignore, ignored=self.note)
        self.emit = emit
        self.fifo = fifo
        self.silence = silence
        self.clock = clock
        self.last_report = {}
        self.last_data = clock()
        self.silent = False

    def note(self, path):
        self.last_report[path] = self.clock()

    def seen(self, report):
        self.note(report["path"])
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
    def __init__(self, baseline, emit, remark, unwatched=()):
        self.baseline = {path: dict(value) for path, value in baseline.items()}
        self.emit = emit
        self.remark = remark
        self.reported = {}
        # An unwatched file (watch: false) has no open sensor, so an in-place rewrite that keeps its size and restores
        # its mtime would pass inode/size/mtime. ctime moves on any write or utime and cannot be set back by its owner,
        # so it is recorded when the agent starts and compared too. Watched files are left out: the polling fallback
        # arms them with utime, which moves ctime by itself.
        self.ctimes = {}
        for path in unwatched:
            try:
                self.ctimes[path] = os.stat(path).st_ctime_ns
            except OSError:
                pass

    def check(self):
        for path, expected in sorted(self.baseline.items()):
            try:
                current = fingerprint(path)
                if path in self.ctimes:
                    current = dict(current, ctime_ns=os.stat(path).st_ctime_ns)
                    expected = dict(expected, ctime_ns=self.ctimes[path])
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


HANDLER_TRIGGERS = {"file-read", "tamper"}
HANDLER_TIMEOUT = 15
HANDLER_MAX_OUTPUT = 64 << 10
HANDLER_GRACE = 60


def handler_is_safe(path):
    """Run an installed handler only if it is a regular, root-owned, non-writable-by-others file."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


class HandlerRunner:
    """Feeds trigger events to an optional installed handler on a bounded worker thread, so a
    slow or missing handler never blocks or crashes the sensor. The handler's JSON stdout,
    if well-formed and small, is delivered as a report through the normal pipeline."""

    def __init__(self, path, emit, run=None, is_safe=handler_is_safe):
        self.path = path
        self.emit = emit
        self.is_safe = is_safe
        self.run = run or self._run
        self.queue = queue.Queue(maxsize=50)
        # Handler processes, as (pid, start) from the same ancestry the sensors report. A report whose
        # reader or any ancestor is one of them came from the handler itself (it opened a watched
        # path): it is the agent's own work, like an open by the agent's pid, so it is neither
        # reported nor offered again, which would otherwise loop. The start token rules out a
        # reused pid; a handler is remembered for HANDLER_GRACE seconds after it exits, because
        # eslogger delivers through a FIFO and can report an open after a short handler is gone.
        self.active = set()
        self.recent = {}  # (pid, start) -> monotonic time it exited
        self.last_activity = None
        self.clock = time.monotonic

    def identities(self):
        now = self.clock()
        for key, ended in list(self.recent.items()):
            if now - ended > HANDLER_GRACE:
                self.recent.pop(key, None)
        return set(self.active) | set(self.recent)

    def owns(self, report):
        known = self.identities()
        if not known:
            return False
        chain = [entry for entry in report.get("ancestry", []) if isinstance(entry, dict)]
        if chain:
            return any((entry.get("pid"), entry.get("start")) in known for entry in chain)
        # No chain: the reader was gone before the sensor looked it up (a delayed eslogger record of a
        # short handler). Fall back to its pid alone, which only matches a handler seen in the grace window.
        return report.get("pid") is not None and report.get("pid") in {pid for pid, _start in known}

    def nearby(self):
        """A handler is running or ran in the last HANDLER_GRACE seconds."""
        return bool(self.active) or (self.last_activity is not None and self.clock() - self.last_activity <= HANDLER_GRACE)

    def offer(self, report):
        if report.get("kind") not in HANDLER_TRIGGERS or not self.is_safe(self.path):
            return
        # A read the sensor could not attribute (the atime poll knows no reader) may be the handler's own:
        # it is still reported, but not handed back to the handler while one is running or just ran.
        if report.get("kind") == "file-read" and "pid" not in report and self.nearby():
            return
        try:
            self.queue.put_nowait(report)
        except queue.Full:
            pass

    def _run(self, payload):
        """Run the handler with the event on stdin. Its stdout is read only up to the cap, it is
        killed at the deadline, and a nonzero exit is a failure; stderr is discarded."""
        deadline = time.monotonic() + HANDLER_TIMEOUT
        # Start gate: the handler is exec'd by a shell that first waits on a pipe, so none of its code runs
        # until its identity is registered (exec keeps the pid and the start time the sensors report).
        gate_read, gate_write = os.pipe()
        try:
            # No preexec_fn (unsafe in this threaded process): the shell reads the gate at its own fd number.
            process = subprocess.Popen(["/bin/sh", "-c", 'read -r _ <&%d || exit 1; exec "$0"' % gate_read, self.path],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       pass_fds=(gate_read,), close_fds=True)
        except BaseException:
            os.close(gate_write)
            raise
        finally:
            os.close(gate_read)
        chain = process_ancestry(process.pid, platform_name())
        identity = (process.pid, chain[0]["start"] if chain else None)
        self.active.add(identity)
        self.last_activity = self.clock()
        os.write(gate_write, b"go\n")
        os.close(gate_write)
        try:
            # Writing the event and reading the result share one deadline, so a handler that
            # never reads its stdin cannot block the writer either.
            stdin, stdout = process.stdin.fileno(), process.stdout.fileno()
            os.set_blocking(stdin, False)
            pending, output = memoryview(payload), b""
            if not pending:
                process.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("timed out after " + str(HANDLER_TIMEOUT) + " seconds")
                readable, writable, _ = select.select([stdout], [stdin] if pending else [], [], remaining)
                if writable:
                    try:
                        pending = pending[os.write(stdin, pending[:1 << 16]):]
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        process.stdin.close()
                if not readable:
                    continue
                chunk = os.read(stdout, HANDLER_MAX_OUTPUT + 1 - len(output))
                if not chunk:
                    break
                output += chunk
                if len(output) > HANDLER_MAX_OUTPUT:
                    raise RuntimeError("output over " + str(HANDLER_MAX_OUTPUT) + " bytes")
            try:
                status = process.wait(max(deadline - time.monotonic(), 0.01))
            except subprocess.TimeoutExpired:
                raise RuntimeError("timed out after " + str(HANDLER_TIMEOUT) + " seconds")
            if status != 0:
                raise RuntimeError("exited with status " + str(status))
            return output
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            self.active.discard(identity)
            self.recent[identity] = self.last_activity = self.clock()
            process.stdout.close()
            if not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass

    def handle(self, report):
        try:
            output = self.run(json.dumps(report).encode("utf-8"))
        except Exception as error:
            self.emit({"kind": "tamper", "reason": "event handler failed", "detail": str(error)[:200]})
            return
        if not output:
            return
        try:
            result = json.loads(output)
        except ValueError:
            self.emit({"kind": "tamper", "reason": "event handler returned non-JSON"})
            return
        # Only the handler's own report kind is delivered: a handler cannot forge sensor
        # reports, and the agent, not the handler, names the host and the time.
        if isinstance(result, dict) and result.get("kind") == "handler":
            result.pop("host", None)
            result.pop("time", None)
            self.emit(result)

    def run_loop(self):
        while True:
            report = self.queue.get()
            try:
                self.handle(report)
            except Exception as error:  # never let one bad result stop the handler thread
                log("event handler result dropped: " + str(error)[:200])


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
    except (FileNotFoundError, PermissionError):
        return default


def write_private(path, data, mode=0o600):
    """Atomically write a root-only file in a root-owned directory (config and state)."""
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


def open_parent(path, owner, chown, created):
    """Open the directory that will hold `path`, one component at a time from '/', creating
    missing ones for `owner`. A registered file usually sits in a tree its owner controls, so
    nothing here is resolved by name after the fact: each step is opened O_NOFOLLOW relative to
    the last, and a symlink is followed only where it sits in a root-owned directory nobody
    else can write (e.g. macOS /var -> /private/var)."""
    fd = os.open("/", DIRECTORY_FLAGS)
    current = ""
    try:
        for name in [part for part in os.path.dirname(path).split("/") if part]:
            current += "/" + name
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(name, 0o700, dir_fd=fd)
                created.append(current)
                child = os.open(name, DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
                if chown:
                    os.fchown(child, owner.pw_uid, owner.pw_gid)
            else:
                if stat.S_ISLNK(st.st_mode):
                    parent = os.fstat(fd)
                    if parent.st_uid != 0 or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or st.st_uid != 0:
                        raise RuntimeError("refusing a symlink in a registered path: " + current)
                    child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
                else:
                    child = os.open(name, DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_existing_parent(path):
    """Like open_parent, for a path that should already exist: walk from '/' one component at a
    time with O_NOFOLLOW (the same narrow symlink exception), create nothing. Raises
    FileNotFoundError if a directory is gone, RuntimeError for a symlink the owner could have made."""
    fd = os.open("/", DIRECTORY_FLAGS)
    current = ""
    try:
        for name in [part for part in os.path.dirname(path).split("/") if part]:
            current += "/" + name
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                parent = os.fstat(fd)
                if parent.st_uid != 0 or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or st.st_uid != 0:
                    raise RuntimeError("refusing a symlink in a registered path: " + current)
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
            else:
                child = os.open(name, DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def placed_unchanged(dirfd, name, sha256):
    """True if `name` in `dirfd` is a regular file (not a symlink) whose content hashes to `sha256`,
    checked on the descriptor actually opened."""
    try:
        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    # O_NONBLOCK: a FIFO swapped in after the stat would otherwise hang this (root) open forever.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return False
        data, chunk = b"", b"x"
        while chunk and len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - len(data))
            data += chunk
    finally:
        os.close(fd)
    return (opened.st_dev, opened.st_ino) == (st.st_dev, st.st_ino) and hashlib.sha256(data).hexdigest() == sha256


def write_placed(dirfd, name, data, mode, owner, chown, stamp, replace_sha256=None):
    """Write one registered file inside `dirfd`. Mode, owner and times are set on the open
    descriptor before it is linked in, and the result is checked to be that same file.

    Nothing an owner made is ever overwritten, even if it appears after the install's checks: a
    new path is linked in with link(2), which fails if the name exists. A path this agent placed
    before (`replace_sha256` is its recorded content) is first renamed aside and checked to be
    that unchanged file; anything else is put back and the install stops."""
    temporary = ".tmp-" + secrets.token_hex(8)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        if chown:
            os.fchown(fd, owner.pw_uid, owner.pw_gid)
        os.fchmod(fd, mode)
        os.utime(fd, (stamp, stamp))
        aside = None
        if replace_sha256 is not None:
            aside = ".old-" + secrets.token_hex(8)
            try:
                os.rename(name, aside, src_dir_fd=dirfd, dst_dir_fd=dirfd)
            except FileNotFoundError:
                aside = None  # gone since the check: nothing to displace
            else:
                if not placed_unchanged(dirfd, aside, replace_sha256):
                    try:
                        os.link(aside, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
                        os.unlink(aside, dir_fd=dirfd)
                    except OSError:
                        raise RuntimeError("a registered path changed while it was being placed; the owner's file is "
                                           "left as " + aside) from None
                    raise RuntimeError("refusing to overwrite an existing file the agent did not place: " + name)
        try:
            os.link(temporary, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
        except BaseException as error:
            if aside is not None:
                # The displaced file is this agent's own, verified unchanged: put it back if the name is
                # free, otherwise remove it, so it is never left hidden under a temporary name.
                try:
                    os.link(aside, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
                except OSError:
                    pass
                os.unlink(aside, dir_fd=dirfd)
            if isinstance(error, FileExistsError):
                raise RuntimeError("refusing to overwrite an existing file the agent did not place: " + name) from None
            raise
        os.unlink(temporary, dir_fd=dirfd)
        if aside is not None:
            os.unlink(aside, dir_fd=dirfd)
        written = os.fstat(fd)
        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        if (st.st_dev, st.st_ino) != (written.st_dev, written.st_ino):
            raise RuntimeError("a registered path changed while it was being placed")
        return {"ino": written.st_ino, "size": written.st_size, "mtime_ns": written.st_mtime_ns}
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=dirfd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def place_files(bundle, previous, chown=True, keygen=generate_ssh_key, save=None):
    """Place registered files. Never overwrites a file this agent did not place unchanged.

    Every owner and template is resolved before the first write. `save` is called with the
    state after each file, so a failure part-way leaves a record: re-running the install
    takes over the files already placed."""
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
    plan = []
    for item in bundle["files"]:
        try:
            owner = pwd.getpwnam(item["owner"])
        except KeyError:
            raise RuntimeError("unknown owner for a registered path: " + item["owner"]) from None
        plan.append((item, owner, render(item["content"], values).encode("utf-8")))
    files, created_dirs = {}, list(previous.get("dirs", []))
    for item, owner, data in plan:
        path = item["path"]
        dirfd = open_parent(path, owner, chown, created_dirs)
        try:
            stamp = time.time() - item.get("age_days", 120) * 86400 - secrets.randbelow(86400)
            stamped = write_placed(dirfd, os.path.basename(path), data, int(item.get("mode", "0600"), 8), owner, chown, stamp,
                                   replace_sha256=placed[path]["sha256"] if path in placed else None)
        finally:
            os.close(dirfd)
        files[path] = {"sha256": hashlib.sha256(data[:MAX_FILE_BYTES + 1]).hexdigest(),
                       "watch": item.get("watch", True), "fingerprint": stamped}
        if save:
            save({"files": dict(placed, **files), "dirs": created_dirs})
    for path in set(placed) - set(files):
        remove_placed(path, placed[path])
    return {"files": files, "dirs": created_dirs}


def remove_placed(path, record):
    """Remove a file this agent placed, only if it is still that unchanged file. Its directories may
    belong to the file's owner, so it is reached through directory descriptors (never by name),
    the check and the unlink use the same directory, and a symlink is never followed."""
    try:
        dirfd = open_existing_parent(path)
    except FileNotFoundError:
        return
    except RuntimeError as error:
        log("left a registered path in place: " + str(error))
        return
    try:
        name = os.path.basename(path)
        try:
            os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if placed_unchanged(dirfd, name, record["sha256"]):
            os.unlink(name, dir_fd=dirfd)
        else:
            log("left a registered path in place because its content changed: " + path)
    finally:
        os.close(dirfd)


def remove_created_dir(path):
    """rmdir a directory the install created, through its parent's descriptor; only if empty."""
    try:
        parent = open_existing_parent(path)
    except (FileNotFoundError, RuntimeError):
        return
    try:
        os.rmdir(os.path.basename(path), dir_fd=parent)
    except OSError:
        pass
    finally:
        os.close(parent)


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


def pause_service(layout, platform):
    """Stop a running agent (not disable or remove it) before an install replaces or reads its files:
    otherwise it reports every replaced inode, and the installer's own reads, as tamper. The
    install starts it again (install_service)."""
    if platform == "linux":
        subprocess.run(["systemctl", "stop", layout.name], capture_output=True)
        state = subprocess.run(["systemctl", "is-active", layout.name], capture_output=True, text=True).stdout.strip()
        still = state in ("active", "activating", "deactivating", "reloading")
    else:
        for label in (layout.label, layout.helper_label):
            subprocess.run(["launchctl", "bootout", "system/" + label], capture_output=True)
        still = subprocess.run(["launchctl", "print", "system/" + layout.label], capture_output=True).returncode == 0
    if still:
        raise SystemExit(layout.name + ": could not stop the running agent; not replacing its files")


def platform_name():
    return "darwin" if sys.platform == "darwin" else "linux"


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("access-audit: run as root")


def marker_text(layout):
    return layout.name + " agent install marker v1\n"


def has_marker(layout):
    """Our own install: a root-owned regular marker file with the expected contents, not a common filename."""
    try:
        st = os.lstat(layout.marker)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_size > 256:
            return False
        with open(layout.marker) as handle:
            return handle.read() == marker_text(layout)
    except OSError:
        return False


def refuse_foreign_dirs(layout):
    """A service name may match a real package (/etc/nginx): never chmod or write into a directory this agent did not make."""
    if has_marker(layout):
        return
    for directory in (layout.config_dir, layout.agent_dir, layout.helper_dir):
        if os.path.lexists(directory):
            raise SystemExit(layout.name + ": " + directory + " exists and was not made by this agent; choose another service_name")


def command_install():
    require_root()
    raw = sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise SystemExit("access-audit: bundle too large")
    bundle = json.loads(raw)
    validate_bundle(bundle)
    layout = Layout(bundle.get("service_name", DEFAULT_SERVICE_NAME), bundle.get("launchd_label"))
    LOG_NAME[0] = layout.name
    refuse_foreign_dirs(layout)
    os.makedirs(layout.config_dir, mode=0o700, exist_ok=True)
    os.chmod(layout.config_dir, 0o700)
    if not has_marker(layout):
        write_private(layout.marker, marker_text(layout))
    previous = load_json(layout.state, {})
    if previous:
        pause_service(layout, platform_name())
    state = place_files(bundle, previous,
                        save=lambda partial: write_private(layout.state, json.dumps(partial, indent=2, sort_keys=True)))
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
        remove_created_dir(directory)
    for path in (layout.config, layout.state, layout.agent, layout.marker):
        if os.path.exists(path):
            os.unlink(path)
    for directory in (layout.agent_dir, layout.config_dir):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    print(layout.name + ": uninstalled")


# --- Service -----------------------------------------------------------------

def make_dispatch(submit, handler):
    """Every sensor and integrity report goes to the reporter and, if it is a trigger, the handler;
    a report caused by the handler's own process tree goes nowhere (see HandlerRunner.owns)."""
    def dispatch(report):
        if handler.owns(report):
            return
        submit(report)
        handler.offer(report)
    return dispatch


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
    handler = HandlerRunner(layout.event_handler, reporter.submit)
    threading.Thread(target=handler.run_loop, name="handler", daemon=True).start()

    dispatch = make_dispatch(reporter.submit, handler)

    watched = sorted(os.path.realpath(path) for path, record in state["files"].items() if record["watch"])
    sensor, degraded = choose_sensor(platform_name(), watched, dispatch, config["ignore_executables"], layout.fifo)
    if degraded:
        reporter.submit({"kind": "tamper", "reason": "sensor degraded to " + sensor.name, "detail": degraded})
    sensor_thread = threading.Thread(target=sensor.run, name="sensor", daemon=True)
    sensor_thread.start()
    integrity = Integrity({path: record["fingerprint"] for path, record in state["files"].items()},
                          dispatch, getattr(sensor, "mark", lambda path: None),
                          unwatched=[path for path, record in state["files"].items() if not record["watch"]])
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
    results = reporter.deliver({"kind": "test", "host": config["host"],
                                "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                "note": "operator-initiated test; no registered path was opened"})
    # The rollout uses this to prove the whole path: every destination must have taken the report.
    failed = sorted(name for name, ok in results.items() if ok is not True)
    if failed:
        raise SystemExit(layout.name + ": test alert NOT delivered to: " + ", ".join(failed))
    print(layout.name + ": test alert sent (log, metrics, email)")


def main(argv):
    commands = {"install": command_install, "uninstall": command_uninstall, "run": command_run,
                "status": command_status, "test-alert": command_test_alert}
    if len(argv) != 2 or argv[1] not in commands:
        raise SystemExit("usage: access-audit.py {" + ",".join(commands) + "}")
    commands[argv[1]]()


if __name__ == "__main__":
    main(sys.argv)

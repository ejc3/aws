import collections
import datetime as dt
import hashlib
from itertools import islice
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

UTC = dt.timezone.utc
HOSTNAME = re.compile(rb"(?<![a-z0-9.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}(?=\.(?![a-z0-9.-])|[^a-z0-9.-]|$)")
ISO_STAMP = re.compile(rb"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
LEGACY = re.compile(rb"^([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}:\d{2}:\d{2})")
NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
DIR = os.O_DIRECTORY | NOFOLLOW
MAX_NODES = 12000
MAX_HASH = 8 * 1024 * 1024
MAX_HASH_TOTAL = 256 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
MAX_LOGS = 8
MAX_GUESTS = 8
MAX_JOURNAL_FILES = 10  # per snapshot
MAX_JOURNAL_BYTES = 128 * 1024 * 1024
MAX_JOURNAL_FILE = 32 * 1024 * 1024
MAX_JOURNAL_OUTPUT = 512 * 1024
MAX_JOURNAL_RECORDS = 600
MACHINE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}")
JOURNAL_ID = re.compile(r"[a-f0-9]{32}")
JOURNAL_EVENTS = {
    b"sshd": (b"Accepted ", "journal_ssh_accepted"),
    b"cron": (b"", "journal_cron"),
    b"sudo": (b"", "journal_sudo"),
    b"systemd": (b"Started ", "journal_service_start"),
}

PERSIST = {
    "system_services": ("etc/systemd/system", "etc/systemd/user", "etc/init.d"),
    "cron": ("etc/cron.d", "etc/cron.hourly", "etc/cron.daily", "etc/cron.weekly", "var/spool/cron/crontabs"),
    "shell_startup": ("etc/profile.d",),
    "system_bins": ("usr/local/bin", "usr/local/sbin"),
    "dynamic_loader": ("etc/ld.so.preload", "etc/ld.so.conf.d"),
    "boot_config": ("etc/modules-load.d", "etc/modprobe.d", "etc/rc.local"),
    "network_hooks": ("etc/network/if-up.d", "etc/networkd-dispatcher", "etc/NetworkManager/dispatcher.d"),
}
USER_PATHS = {
    "user_services": (".config/systemd/user",),
    "user_startup": (".config/autostart", ".profile", ".bashrc", ".zshrc", ".bash_profile", ".zprofile"),
}
LOG_SELECT = re.compile(r"^(syslog|auth\.log|kern\.log|cloud-init\.log|dpkg\.log)(?:\.[0-9]+)?$")
CASES = {
    "ssh_accepted": re.compile(rb"sshd\[.*?\]: Accepted "),
    "cron": re.compile(rb"CRON\["),
    "sudo": re.compile(rb"sudo\[|sudo:\s"),
    "service_change": re.compile(rb"systemd\[1\].*?(Started |Stopping |Reloaded |Failed to start )"),
    "package": re.compile(rb"\d\d:\d\d:\d\d (install|upgrade|configure) "),
}

def ancestor(fd, parts):
    current = os.dup(fd)
    try:
        for piece in parts:
            nxt = os.open(piece, DIR, dir_fd=current)
            os.close(current)
            current = nxt
        return current
    except OSError:
        os.close(current)
        return None

def parse_time(raw):
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Query:
    def __init__(self, config):
        self.start = parse_time(config["window_start_utc"])
        self.end = parse_time(config["window_end_utc"])
        self.ioc = config.get("hostname_sha256")
        if not dt.timedelta(0) < self.end - self.start <= dt.timedelta(hours=24):
            raise ValueError("Expected a bounded UTC window")

    def timed(self, timestamp):
        return self.start.timestamp() <= timestamp <= self.end.timestamp()

    def ioc_hit(self, data):
        if not self.ioc:
            return False
        return any(hashlib.sha256(match.group()).hexdigest() == self.ioc for match in HOSTNAME.finditer(data.lower()))

    def log_time(self, line):
        match = ISO_STAMP.match(line)
        try:
            if match:
                return parse_time(match.group().decode("ascii"))
            match = LEGACY.match(line)
            if match:
                value = match.group().decode("ascii")
                for year in {self.start.year, self.end.year}:
                    stamp = dt.datetime.strptime(f"{year} {value}", "%Y %b %d %H:%M:%S").replace(tzinfo=UTC)
                    if self.start <= stamp <= self.end:
                        return stamp
        except ValueError:
            pass
        return None

def read_regular(parent, name, size):
    fd = os.open(name, NOFOLLOW, dir_fd=parent)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode):
            return b""
        with os.fdopen(os.dup(fd), "rb") as f:
            return f.read(size)
    finally:
        os.close(fd)

def entries(parent, cap, coverage, metric="omitted_directory_entries", accept=lambda name: True):
    # Bound examined names, including ones rejected by the filter, before sorting.
    with os.scandir(parent) as iterator:
        names = [item.name for item in islice(iterator, cap + 1)]
    if len(names) > cap:
        coverage[metric] += 1
    return sorted(name for name in names[:cap] if accept(name))


def summarize_journal(stream, query, counts):
    for line in islice(stream, MAX_JOURNAL_RECORDS):
        try:
            event = json.loads(line)
            message = event.get("MESSAGE")
            ident = event.get("SYSLOG_IDENTIFIER")
            if not isinstance(message, str):
                continue
        except (ValueError, AttributeError):
            counts["journal_invalid_output"] += 1
            continue
        message = message.encode("utf-8", "replace")
        ident = ident.lower().encode("utf-8", "replace") if isinstance(ident, str) else b""
        counts["journal_records_checked"] += 1
        if query.ioc_hit(message):
            counts["journal_ioc_exact"] += 1
        if ident in JOURNAL_EVENTS:
            prefix, label = JOURNAL_EVENTS[ident]
            if message.startswith(prefix):
                counts[label] += 1


class Journal:
    def __init__(self, scratch):
        self.scratch = scratch
        self.files = 0
        self.size = 0
        self.deadline = time.monotonic() + 180

    def inspect(self, base, query, counts, file_cap):
        if self.scratch is None:
            return
        journal = ancestor(base, ["var", "log", "journal"])
        if journal is None:
            return
        root_files = 0
        try:
            for namespace in entries(journal, 16, counts, accept=JOURNAL_ID.fullmatch):
                handle = ancestor(journal, [namespace])
                if handle is None:
                    continue
                try:
                    candidates = []
                    for name in entries(handle, 64, counts, accept=lambda n: n.endswith((".journal", ".journal~"))):
                        info = os.stat(name, dir_fd=handle, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode):
                            candidates.append((name, info))
                    # Favor system journals likely to span the selected window;
                    # leave at least one file slot per later guest when possible.
                    candidates.sort(key=lambda x: (not x[0].startswith("system"),
                                                   abs(x[1].st_mtime - query.end.timestamp())))
                    for name, info in candidates:
                        if root_files >= file_cap:
                            counts["journal_root_file_cap"] += 1
                            return
                        if self.read(handle, name, info.st_size, query, counts):
                            root_files += 1
                except OSError:
                    counts["journal_inaccessible"] += 1
                finally:
                    os.close(handle)
        finally:
            os.close(journal)

    def read(self, parent, name, size, query, counts):
        if not 0 < size <= MAX_JOURNAL_FILE:
            counts["journal_size_skipped"] += 1
            return False
        remaining = self.deadline - time.monotonic()
        if self.files >= MAX_JOURNAL_FILES or self.size + size > MAX_JOURNAL_BYTES or remaining < 1:
            counts["journal_budget_reached"] += 1
            return False
        self.files += 1
        self.size += size
        # Never pass snapshot paths, filenames, environment or options to the
        # parser. It receives one sanitized copy readable by its unprivileged UID.
        # The private mount parent is inaccessible to that UID.
        with tempfile.TemporaryDirectory(prefix="journal-", dir=self.scratch) as scratch:
            os.chmod(scratch, 0o755)
            path = os.path.join(scratch, "sample.journal")
            try:
                source = os.open(name, NOFOLLOW, dir_fd=parent)
                with os.fdopen(source, "rb") as input_file, open(path, "wb") as output_file:
                    found = os.fstat(input_file.fileno())
                    if not stat.S_ISREG(found.st_mode) or found.st_size != size:
                        counts["journal_inaccessible"] += 1
                        return True
                    shutil.copyfileobj(input_file, output_file, length=128 * 1024)
                os.chmod(path, 0o444)
                with tempfile.TemporaryFile() as summary:
                    result = subprocess.run(
                        ["/usr/bin/prlimit", "--as=402653184", "--cpu=6:8",
                         f"--fsize={MAX_JOURNAL_OUTPUT}", "--", "/usr/bin/unshare", "--net", "--",
                         "/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs",
                         "/usr/bin/journalctl", "--no-pager", "--quiet", "--utc", "--output=json",
                         "--output-fields=MESSAGE,SYSLOG_IDENTIFIER",
                         f"--lines={MAX_JOURNAL_RECORDS}", f"--file={path}",
                         f"--since=@{int(query.start.timestamp())}", f"--until=@{int(query.end.timestamp())}"],
                        cwd=scratch, env={"LANG": "C", "SYSTEMD_PAGERSECURE": "1"},
                        stdout=summary, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                        timeout=min(10, remaining), check=False,
                    )
                    if result.returncode:
                        counts["journal_parser_failed"] += 1
                        return True
                    summary.seek(0)
                    counts["journal_files_checked"] += 1
                    summarize_journal(summary, query, counts)
            except (OSError, subprocess.TimeoutExpired):
                counts["journal_parser_failed"] += 1
        return True


def scan(root, query, journal):
    base = os.open(root, DIR)
    results = collections.defaultdict(dict)
    coverage = collections.Counter()
    events = collections.defaultdict(collections.Counter)
    try:
        def observe(parent, leaf, scope, category, path, depth):
            if coverage["examined"] >= MAX_NODES or depth > 16:
                coverage["omitted_cap"] += 1
                return
            try:
                s = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except OSError:
                coverage["inaccessible"] += 1
                return
            coverage["examined"] += 1
            if stat.S_ISDIR(s.st_mode):
                handle = ancestor(parent, [leaf])
                if handle is None:
                    coverage["inaccessible"] += 1
                    return
                try:
                    for child in entries(handle, min(1024, MAX_NODES - coverage["examined"]), coverage):
                        observe(handle, child, scope, category, path + (child,), depth + 1)
                finally:
                    os.close(handle)
                return
            hits = False
            try:
                if stat.S_ISLNK(s.st_mode):
                    digest = hashlib.sha256(os.fsencode(os.readlink(leaf, dir_fd=parent))).digest()
                elif stat.S_ISREG(s.st_mode):
                    if s.st_size > MAX_HASH or coverage["hashed_bytes"] + s.st_size > MAX_HASH_TOTAL:
                        coverage["files_metadata_only"] += 1
                        digest = None
                    else:
                        coverage["hashed_bytes"] += s.st_size
                        contents = read_regular(parent, leaf, MAX_HASH + 1)
                        digest = hashlib.sha256(contents).digest()
                        hits = query.ioc_hit(contents)
                else:
                    coverage["special_files_skipped"] += 1
                    return
            except OSError:
                coverage["inaccessible"] += 1
                return
            results[scope, category][path] = (s.st_mode, s.st_uid, s.st_gid, s.st_size, digest,
                                               query.timed(s.st_mtime), hits, s.st_mtime_ns)

        def from_root(fd, relative, scope, category, prefix=()):
            pieces = relative.split("/")
            parent = ancestor(fd, pieces[:-1])
            if parent is None:
                return
            try:
                try:
                    os.stat(pieces[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                observe(parent, pieces[-1], scope, category, prefix + tuple(pieces), 0)
            finally:
                os.close(parent)

        def inspect_root(fd, scope, prefix=()):
            for category, paths in PERSIST.items():
                for relative in paths:
                    from_root(fd, relative, scope, category, prefix)
            for parent in (("root", "home") if scope == "host" else ("root",)):
                holder = ancestor(fd, [parent])
                if holder is None:
                    continue
                try:
                    users = entries(holder, 32, coverage) if parent == "home" else [None]
                    for user in users:
                        userfd = ancestor(holder, [user]) if user is not None else os.dup(holder)
                        if userfd is None:
                            continue
                        try:
                            for category, paths in USER_PATHS.items():
                                for relative in paths:
                                    from_root(userfd, relative, scope, category, prefix + (parent, user or ""))
                        finally:
                            os.close(userfd)
                finally:
                    os.close(holder)
            logs = ancestor(fd, ["var", "log"])
            if logs is not None:
                try:
                    names = entries(logs, 256, coverage, accept=LOG_SELECT.fullmatch)
                    if len(names) > MAX_LOGS:
                        coverage["plain_log_file_cap"] += 1
                    for name in names[:MAX_LOGS]:
                        plain_log(logs, name, query, events[scope])
                finally:
                    os.close(logs)
            journal.inspect(fd, query, events[scope], 2 if scope == "host" else 1)

        inspect_root(base, "host")
        machines = ancestor(base, ["var", "lib", "machines"])
        if machines is not None:
            try:
                names = entries(machines, 32, coverage, accept=MACHINE_NAME.fullmatch)
                for name in names:
                    guest = ancestor(machines, [name])
                    if guest is None:
                        continue
                    try:
                        if coverage["guest_roots"] >= MAX_GUESTS:
                            coverage["guest_root_cap"] += 1
                            break
                        coverage["guest_roots"] += 1
                        inspect_root(guest, "guest", (name,))
                    finally:
                        os.close(guest)
            finally:
                os.close(machines)
        return results, coverage, events
    finally:
        os.close(base)


def plain_log(logs, name, query, counts):
    s = os.stat(name, dir_fd=logs, follow_symlinks=False)
    if not stat.S_ISREG(s.st_mode):
        return
    fd = os.open(name, NOFOLLOW, dir_fd=logs)
    with os.fdopen(fd, "rb") as f:
        f.seek(max(0, s.st_size - MAX_LOG))
        block = f.read(MAX_LOG)
    counts["plain_logs_checked"] += 1
    if s.st_size > MAX_LOG:
        counts["partial_log_tails"] += 1
    for line in block.splitlines():
        stamp = query.log_time(line[:40])
        if stamp is None or not query.start <= stamp <= query.end:
            continue
        counts["time_matched_lines"] += 1
        if query.ioc_hit(line):
            counts["ioc_in_log"] += 1
        for label, pattern in CASES.items():
            if pattern.search(line):
                counts[label] += 1


def compare(pre_root, post_root, query, journal_scratch=None):
    # Split the aggregate journal budget evenly so busy pre logs cannot use up
    # the post budget (or vice versa).
    pre, pre_coverage, pre_events = scan(pre_root, query, Journal(journal_scratch))
    post, post_coverage, post_events = scan(post_root, query, Journal(journal_scratch))
    diff = {"host": {}, "guest": {}}
    for scope, category in sorted(pre.keys() | post.keys()):
        key = scope, category
        a, b = pre.get(key, {}), post.get(key, {})
        new = b.keys() - a.keys()
        removed = a.keys() - b.keys()
        changed = {k for k in b.keys() & a.keys() if b[k][:5] != a[k][:5]
                   or (b[k][4] is None and b[k][7] != a[k][7])}
        around = sum(b[k][5] for k in new | changed)
        hits = sum(v[6] for v in b.values())
        if new or removed or changed or hits:
            diff[scope][category] = {"new": len(new), "changed": len(changed), "removed": len(removed),
                                     "changed_mtime_in_window": around, "post_ioc_exact": hits}
    return {"comparison": diff, "pre_coverage": pre_coverage, "post_coverage": post_coverage,
            "pre_log_counts": pre_events, "post_log_counts": post_events,
            "coverage": "selected host and guest persistence, bounded plain-text tails and persistent journal samples"}

if __name__ == "__main__":
    with open(sys.argv[3], encoding="utf-8") as config:
        print(json.dumps(compare(sys.argv[1], sys.argv[2], Query(json.load(config)), sys.argv[4]), sort_keys=True))

import collections
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys

UTC = dt.timezone.utc
HOSTNAME = re.compile(rb"(?<![a-z0-9.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}(?![a-z0-9.-])")
ISO_STAMP = re.compile(rb"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
LEGACY = re.compile(rb"^([A-Z][a-z]{2})\s+(\d{1,2}) (\d{2}:\d{2}:\d{2})")
NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
DIR = os.O_DIRECTORY | NOFOLLOW
MAX_NODES = 12000
MAX_HASH = 8 * 1024 * 1024
MAX_LOG = 4 * 1024 * 1024
MAX_LOGS = 24
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

def scan(root, query):
    base = os.open(root, DIR)
    results = collections.defaultdict(dict)
    coverage = collections.Counter()
    try:
        def observe(parent, leaf, category, path, depth):
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
                    for child in sorted(os.listdir(handle))[:MAX_NODES]:
                        observe(handle, child, category, path + (child,), depth + 1)
                finally:
                    os.close(handle)
                return
            hits = False
            try:
                if stat.S_ISLNK(s.st_mode):
                    digest = hashlib.sha256(os.fsencode(os.readlink(leaf, dir_fd=parent))).digest()
                elif stat.S_ISREG(s.st_mode):
                    if s.st_size > MAX_HASH:
                        coverage["large_files_metadata_only"] += 1
                        digest = None
                    else:
                        contents = read_regular(parent, leaf, MAX_HASH + 1)
                        digest = hashlib.sha256(contents).digest()
                        hits = query.ioc_hit(contents)
                else:
                    coverage["special_files_skipped"] += 1
                    return
            except OSError:
                coverage["inaccessible"] += 1
                return
            results[category][path] = (s.st_mode, s.st_uid, s.st_gid, s.st_size, digest, query.timed(s.st_mtime), hits, s.st_mtime_ns)

        def from_root(fd, relative, category, prefix=()):
            pieces = relative.split("/")
            parent = ancestor(fd, pieces[:-1])
            if parent is None:
                return
            try:
                try:
                    os.stat(pieces[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                observe(parent, pieces[-1], category, prefix + tuple(pieces), 0)
            finally:
                os.close(parent)

        for category, paths in PERSIST.items():
            for relative in paths:
                from_root(base, relative, category)
        for parent in ("root", "home"):
            holder = ancestor(base, [parent])
            if holder is None:
                continue
            try:
                users = sorted(os.listdir(holder))[:32] if parent == "home" else [None]
                for user in users:
                    userfd = ancestor(holder, [user]) if user is not None else os.dup(holder)
                    if userfd is None:
                        continue
                    try:
                        for category, paths in USER_PATHS.items():
                            for relative in paths:
                                from_root(userfd, relative, category, (parent, user or ""))
                    finally:
                        os.close(userfd)
            finally:
                os.close(holder)

        log_counts = collections.Counter()
        logs = ancestor(base, ["var", "log"])
        if logs is not None:
            try:
                for name in [n for n in sorted(os.listdir(logs)) if LOG_SELECT.fullmatch(n)][:MAX_LOGS]:
                    s = os.stat(name, dir_fd=logs, follow_symlinks=False)
                    if not stat.S_ISREG(s.st_mode):
                        continue
                    fd = os.open(name, NOFOLLOW, dir_fd=logs)
                    try:
                        os.lseek(fd, max(0, s.st_size - MAX_LOG), os.SEEK_SET)
                        with os.fdopen(os.dup(fd), "rb") as f:
                            block = f.read(MAX_LOG)
                    finally:
                        os.close(fd)
                    log_counts["plain_logs_checked"] += 1
                    if s.st_size > MAX_LOG:
                        log_counts["partial_log_tails"] += 1
                    for line in block.splitlines():
                        stamp = query.log_time(line[:40])
                        if stamp is None or not query.start <= stamp <= query.end:
                            continue
                        log_counts["time_matched_lines"] += 1
                        if query.ioc_hit(line):
                            log_counts["ioc_in_log"] += 1
                        for label, pattern in CASES.items():
                            if pattern.search(line):
                                log_counts[label] += 1
            finally:
                os.close(logs)
        return results, coverage, log_counts
    finally:
        os.close(base)

def compare(pre_root, post_root, query):
    pre, pre_coverage, pre_events = scan(pre_root, query)
    post, post_coverage, post_events = scan(post_root, query)
    diff = {}
    for category in sorted(pre.keys() | post.keys()):
        a, b = pre.get(category, {}), post.get(category, {})
        new = b.keys() - a.keys()
        removed = a.keys() - b.keys()
        changed = {k for k in b.keys() & a.keys() if b[k][:5] != a[k][:5]
                   or (b[k][4] is None and b[k][7] != a[k][7])}
        around = sum(b[k][5] for k in new | changed)
        hits = sum(v[6] for v in b.values())
        if new or removed or changed or hits:
            diff[category] = {"new": len(new), "changed": len(changed), "removed": len(removed),
                              "changed_mtime_in_window": around, "post_ioc_exact": hits}
    return {"comparison": diff, "pre_coverage": pre_coverage, "post_coverage": post_coverage,
            "pre_log_counts": pre_events, "post_log_counts": post_events,
            "coverage": "selected persistence paths and bounded plain-text log tails"}

if __name__ == "__main__":
    with open(sys.argv[3], encoding="utf-8") as config:
        print(json.dumps(compare(sys.argv[1], sys.argv[2], Query(json.load(config))), sort_keys=True))

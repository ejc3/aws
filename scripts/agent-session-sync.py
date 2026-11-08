#!/usr/bin/env python3
"""agent-session-sync -- make a NEW repository show up in Claude Code and Codex within seconds.

Installed as /usr/local/bin/agent-session-sync and run as a small service (agent-session-sync.service,
or agent-session-sync@<user>.service on nextjs-dev) on every box that keeps remote-control sessions.
It replaces "the boot launcher looked once, at boot" with a cheap watch loop (default every 5 s).

WHAT COUNTS AS NEW. A repository that appears after the watcher first looked:
  * every checkout directly under one of the roots (default ~/* and ~/src/*) whose `.git` is a
    directory: a clone or a new repository gets a session on its own (owner, 2026-10-27: "clone is fine,
    I just don't want one for every internal worktree that agents create");
  * a linked worktree of such a checkout, wherever it lives (git records it in .git/worktrees/<name>),
    only when it is marked for interactive use: the file `claude-interactive` in its git directory
    (`touch "$(git rev-parse --git-dir)/claude-interactive"`), which agents do for a worktree the user
    wants to prompt; a subagent's worktree under .claude/worktrees/ never counts;
  * finished: HEAD resolves, no *.lock in .git (a clone, fetch or checkout is still running) and the
    index exists (a clone writes it last), so a half-cloned repo is not launched;
  * allowed: its origin is owned by an allowed owner (the account's own GitHub login, plus
    ~/.config/agent-session-sync/owners and --owner) or is an exact --repo, and it is not claude-code-sync.
On administration jumpboxes, --require-clone-marker also requires claude-interactive for a main
checkout before either agent starts. An allowed origin alone does not approve repository startup hooks.
The FIRST run only records what already exists and launches nothing: the boot launcher decided about
those. A state file written before every clone counted (no "clones" key) is adopted the same way: the
first scan with this version records the unmarked checkouts already there without launching them, so the
upgrade does not open a session in every old clone at once.

AFTER A REBOOT it starts Claude in every folder agent-active-dirs names (a person prompted there, or it was
marked, in the last 30 days): the same rule the boot launchers use, so a folder nobody prompted stays closed.
On a box with a boot launcher those windows already exist and t-claude only attaches; on the jumpboxes,
which have none, this is the boot launch.

WHAT IT DOES for a new repo: pre-accept Claude's per-folder trust prompt (only for a repo that passed
the gates above), start `t-claude --auto --remote-control` there (a window in the user's EXISTING tmux
server; with no server yet it waits rather than own one), and seed a Codex thread (detached: a seed
turn can take minutes, the thread itself exists at once). Claude and Codex are tracked separately, so
a Codex that is not logged in yet does not hold up Claude and is retried.

COST. Per tick: one listing and one stat per entry in the roots. Anything already handled is skipped
before any deeper check, so an idle box does almost nothing. Separately, the watcher retires
unused unattended Claude windows after one week and records the decision per checkout.
"""
import argparse
import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

ORIGIN_FORMS = [
    re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$"),
]
NEVER_REPOS = {"claude-code-history", "claude-code-sync"}
RETRY_SECONDS = 60


def log(msg):
    print("agent-session-sync: " + msg, flush=True)


_LOGGED = {}


def log_rarely(key, msg, gap=600, now=None):
    """A retry that cannot succeed yet (t-claude not installed, not logged in) is one line per gap, not one per tick."""
    now = time.time() if now is None else now
    if now - _LOGGED.get(key, -gap) >= gap:
        _LOGGED[key] = now
        log(msg)


# ------------------------------------------------------------------ discovery

def parse_origin(url):
    """(owner, repo) for a github.com origin in any of the three forms, else None."""
    url = (url or "").strip()
    for form in ORIGIN_FORMS:
        m = form.match(url)
        if m:
            return m.group(1), m.group(2)
    return None


def read_origin(git_dir):
    """The origin url from .git/config, read as text (no subprocess)."""
    try:
        text = open(os.path.join(git_dir, "config"), encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    in_origin = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_origin = re.match(r'^\[remote\s+"origin"\]$', line) is not None
        elif in_origin:
            m = re.match(r"^url\s*=\s*(.+)$", line)
            if m:
                return m.group(1).strip()
    return None


MARKER = "claude-interactive"


def git_dirs(path):
    """(git dir, common git dir) of a checkout or a linked worktree, or (None, None)."""
    dot = os.path.join(path, ".git")
    if os.path.isdir(dot) and not os.path.islink(dot):
        return dot, dot
    try:
        with open(dot) as fh:
            line = fh.read().strip()
    except OSError:
        return None, None
    if not line.startswith("gitdir:"):
        return None, None
    gitdir = line[len("gitdir:"):].strip()
    gitdir = gitdir if os.path.isabs(gitdir) else os.path.normpath(os.path.join(path, gitdir))
    try:
        with open(os.path.join(gitdir, "commondir")) as fh:
            common = fh.read().strip()
        common = common if os.path.isabs(common) else os.path.normpath(os.path.join(gitdir, common))
    except OSError:
        common = gitdir
    return gitdir, common


def is_main_checkout(path):
    """`.git` is a directory. A linked worktree (or a submodule) has a `.git` FILE."""
    git = os.path.join(path, ".git")
    return os.path.isdir(git) and not os.path.islink(git)


def is_finished(path):
    git, _ = git_dirs(path)
    if git is None:
        return False
    try:
        names = os.listdir(git)
    except OSError:
        return False
    if "HEAD" not in names or "index" not in names:
        return False
    if any(n.endswith(".lock") for n in names):
        return False
    r = subprocess.run(["git", "-C", path, "rev-parse", "-q", "--verify", "HEAD"],
                       capture_output=True, text=True, timeout=20)
    return r.returncode == 0


def is_allowed(path, owners, repos):
    _, common = git_dirs(path)
    origin = parse_origin(read_origin(common)) if common else None
    if not origin:
        return False
    owner, repo = origin
    if repo in NEVER_REPOS:
        return False
    return owner.lower() in owners or "%s/%s" % (owner, repo) in repos


def candidates(roots, require_clone_marker=False):
    """Main checkouts (marked too on admin boxes) and marked linked worktrees."""
    seen = set()
    for pattern in roots:
        for p in glob.glob(pattern):
            name = os.path.basename(p)
            if name.startswith("."):
                continue
            real = os.path.realpath(p)
            if not is_main_checkout(real):
                continue
            git = os.path.join(real, ".git")
            if real not in seen and (not require_clone_marker or os.path.exists(os.path.join(git, MARKER))):
                seen.add(real)
                yield real
            for admin in sorted(glob.glob(os.path.join(git, "worktrees", "*"))):
                if not os.path.exists(os.path.join(admin, MARKER)):
                    continue
                try:
                    with open(os.path.join(admin, "gitdir")) as fh:
                        wt = os.path.realpath(os.path.dirname(fh.read().strip()))
                except OSError:
                    continue
                if wt in seen or "/.claude/worktrees/" in wt + "/" or not os.path.isdir(wt):
                    continue
                seen.add(wt)
                yield wt


def github_login(home):
    """The account's own GitHub login, from gh's hosts.yml (no network)."""
    try:
        text = open(os.path.join(home, ".config", "gh", "hosts.yml"), encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"^\s+user:\s*(\S+)\s*$", text, re.M)
    return m.group(1) if m else None


def allowed_owners(home, extra):
    owners = {o.lower() for o in extra}
    login = github_login(home)
    if login:
        owners.add(login.lower())
    try:
        with open(os.path.join(home, ".config", "agent-session-sync", "owners"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    owners.add(line.lower())
    except OSError:
        pass
    return owners


def owners_signature(home):
    """What the allowed owners are derived from: gh's hosts.yml and the owners file."""
    sig = []
    for p in (os.path.join(home, ".config", "gh", "hosts.yml"), os.path.join(home, ".config", "agent-session-sync", "owners")):
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def refresh_owners(cfg):
    """The account logs in to gh, or someone edits the owners file, while the watcher runs: pick it up. Two
    stats per tick. Returns True when the allowed owners changed."""
    if "owners_extra" not in cfg:
        return False
    sig = owners_signature(cfg["home"])
    if cfg.get("owners_sig") == sig:
        return False
    owners = allowed_owners(cfg["home"], cfg["owners_extra"])
    first = "owners_sig" not in cfg
    cfg["owners_sig"] = sig
    if owners == cfg.get("owners"):
        return False
    cfg["owners"] = owners
    if not first:
        log("allowed owners are now %s" % sorted(owners))
    return not first


def repo_identity(path):
    """[device, inode] of the checkout's .git directory: stable while the checkout lives (git never replaces
    the directory itself), different for a replacement clone at the same path."""
    git, _ = git_dirs(path)
    if git is None:
        return None
    try:
        st = os.stat(git)
    except OSError:
        return None
    return [st.st_dev, st.st_ino]


# ----------------------------------------------------- unattended Claude session retirement

RETIRE_AFTER = 7 * 24 * 60 * 60
RETIRE_SCAN = 60
RETIRED_MARKER = "claude-remote-retired"
UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
# A Claude-written transcript may contain bookkeeping before its first turn. Unknown
# records count as activity: the CLI can add types without coordinating with this script.
EMPTY_RECORDS = {"file-history-snapshot", "mode", "permission-mode", "session-owner", "bridge-session", "atis-latch"}
USAGE_FIELDS = {"totalAPIDuration", "totalAPIDurationWithoutRetries", "totalCostUSD",
                "totalLinesAdded", "totalLinesRemoved", "totalToolDuration"}
COST_FIELDS = USAGE_FIELDS | {"type", "sessionId", "startTime", "totalDuration", "modelUsage", "hasUnknownModelCost"}

def retirement_marker(path, home):
    """A tombstone belongs to this checkout/worktree, or to this non-Git directory."""
    path = os.path.realpath(path)
    git, _ = git_dirs(path)
    if git is not None and os.path.isdir(git):
        return os.path.join(git, RETIRED_MARKER)
    inode = os.stat(path).st_ino
    key = hashlib.sha256(os.fsencode(path) + b"\0" + str(inode).encode()).hexdigest()
    return os.path.join(home, ".local", "state", "agent-session-sync", "retired", key)


def claude_root(home):
    return os.path.realpath(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude"))


def claude_retired(path, home):
    try:
        marker = retirement_marker(path, home)
        os.lstat(marker)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        intent = read_intent(marker)
    except (OSError, ValueError):
        return True  # an unreadable marker never authorizes automatic startup
    if intent is None:
        return True  # completed (or explicitly created) tombstone
    # A reboot can separate the durable intent from the close. Re-read the exact
    # transcript: a message delivered to the surviving session overrides the intent.
    empty, exists, _ = transcript_evidence(intent["transcript"], claude_root(home), intent["sid"])
    return empty and (exists or intent["source"] == "startup")


def regular_read(path, limit):
    """Open only a bounded, regular file. Never follow a leaf symlink or read a pipe."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("not a small regular file")
        data = fh.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file grew beyond its limit")
        return data


def transcript_evidence(path, root, sid):
    """Return (empty, existing, earliest bridge timestamp) for the exact native transcript."""
    if not UUID.fullmatch(sid) or not os.path.isabs(path):
        return False, False, None
    expected = os.path.join(os.path.realpath(root), "projects")
    path = os.path.normpath(path)
    if (os.path.basename(path) != sid + ".jsonl"
            or os.path.dirname(os.path.dirname(path)) != expected
            or os.path.realpath(path) != path):
        return False, False, None
    bridge_times, metadata_ids, last_pointers = [], set(), set()
    try:
        data = regular_read(path, 1024 * 1024)
        for line in data.splitlines():
            row = json.loads(line)
            if (not isinstance(row, dict) or row.get("sessionId") not in (None, sid)
                    or any(k in row for k in ("message", "prompt", "role"))):
                return False, True, None
            kind = row.get("type")
            if kind in EMPTY_RECORDS:
                if kind == "file-history-snapshot" and (row.get("snapshot") or {}).get("trackedFileBackups"):
                    return False, True, None
            elif (kind, row.get("subtype")) == ("system", "bridge_status"):
                when = row.get("timestamp")
                if isinstance(when, str) and row.get("sessionId") == sid:
                    timestamp = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
                    if timestamp.tzinfo is not None:
                        bridge_times.append(timestamp.timestamp())
            elif kind == "cost-state" and (set(row) <= COST_FIELDS
                    and all(row.get(k) == 0 for k in USAGE_FIELDS)
                    and row.get("modelUsage") == {} and row.get("hasUnknownModelCost") is False):
                pass
            elif kind == "last-prompt" and set(row) <= {"type", "sessionId", "leafUuid"}:
                if row.get("leafUuid"):
                    last_pointers.add(row["leafUuid"])
            else:
                return False, True, None
            if isinstance(row.get("uuid"), str):
                metadata_ids.add(row["uuid"])
    except FileNotFoundError:
        # A new SessionStart can report its path before Claude writes the first record.
        return True, False, None
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return False, False, None
    return last_pointers <= metadata_ids, True, min(bridge_times, default=None)


def empty_transcript(path, root, sid):
    return transcript_evidence(path, root, sid)[0]


def linux_process(pid):
    """Return PID, parent and boot-relative start ticks for this account's live process."""
    path = f"/proc/{pid}"
    try:
        if os.stat(path).st_uid != os.getuid():
            return None
        with open(path + "/stat") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        if fields[0] in ("T", "t", "Z", "X"):
            return None                         # stopped or exited, not an idle foreground Claude
        return int(fields[1]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_intent(marker):
    data = regular_read(marker, 8192)
    if data in (b"", b"retired\n"):
        return None
    intent = json.loads(data)
    if (not isinstance(intent, dict) or set(intent) != {"launch", "sid", "source", "transcript"}
            or not all(isinstance(v, str) for v in intent.values())
            or not UUID.fullmatch(intent["launch"]) or not UUID.fullmatch(intent["sid"])
            or intent["source"] not in ("startup", "resume")):
        raise ValueError("invalid retirement intent")
    return intent


def write_marker(marker, data, replace=False):
    directory = os.path.dirname(marker)
    fd, tmp = tempfile.mkstemp(prefix=".claude-retirement.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if replace:
            os.replace(tmp, marker)
        else:
            os.link(tmp, marker)               # atomically refuse an existing marker
        sync_directory(directory)
        sync_directory(os.path.dirname(directory))
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def retirement_intent(marker, launch, sid, source, transcript):
    """Publish a recoverable intent without overwriting a pre-existing tombstone."""
    intent = dict(launch=launch, sid=sid, source=source, transcript=transcript)
    os.makedirs(os.path.dirname(marker), mode=0o700, exist_ok=True)
    try:
        write_marker(marker, json.dumps(intent, ensure_ascii=False).encode() + b"\n")
    except FileExistsError:
        # After a watcher crash only the same exact launch may retry that close.
        if read_intent(marker) != intent:
            return None
    return os.lstat(marker)


def cancel_intent(marker, marker_info):
    try:
        if os.path.samestat(os.lstat(marker), marker_info):
            os.unlink(marker)
            sync_directory(os.path.dirname(marker))
    except FileNotFoundError:
        pass


def linux_uptime():
    with open("/proc/uptime") as fh:
        return float(fh.read().split()[0])


def idle_claude(root, pane_pid, sid, folder, now):
    """Find the exact native Claude child, using the CLI's own PID/UUID/idle record."""
    candidates = []
    try:
        with os.scandir(os.path.join(root, "sessions")) as entries:
            records = list(entries)
        uptime = linux_uptime()
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return None
    for entry in records:
        if not re.fullmatch(r"[1-9][0-9]*\.json", entry.name):
            continue
        pid = int(entry.name[:-5])
        try:
            meta = json.loads(regular_read(entry.path, 32 * 1024))
            if (not isinstance(meta, dict) or meta.get("pid") != pid or meta.get("sessionId") != sid
                    or meta.get("status") != "idle" or type(meta.get("procStart")) is not int
                    or type(meta.get("startedAt")) is not int or type(meta.get("statusUpdatedAt")) is not int
                    or not isinstance(meta.get("cwd"), str) or os.path.realpath(meta["cwd"]) != folder):
                continue
        except (OSError, ValueError, TypeError):
            continue
        process = linux_process(pid)
        if not process or process[1] != meta["procStart"]:
            continue
        try:
            # The pathname in the registry can now name a replacement checkout.
            # Compare the directory the live process actually has open.
            if not os.path.samestat(os.stat(f"/proc/{pid}/cwd"), os.stat(folder)):
                continue
        except OSError:
            continue
        old_process = now - meta["startedAt"] / 1000 > RETIRE_AFTER and uptime - process[1] / hz > RETIRE_AFTER
        parent = process[0]
        for _ in range(64):
            if parent <= 1:
                break
            if parent == pane_pid:
                candidates.append((pid, process[1], meta["statusUpdatedAt"], old_process))
                break
            p = linux_process(parent)
            if p is None:
                break
            parent = p[0]
    return candidates[0] if len(candidates) == 1 else None


class ClaudeRetirement:
    """Two idle observations, then retire exactly one unattended window under the launch lock."""

    FIELDS = ("window_id", "pane_id", "pane_pid", "window_panes", "window_active_clients",
              "@tclaude_auto_rc", "@tclaude_auto_rc_started", "@tclaude_auto_rc_sid",
              "@tclaude_auto_rc_used", "@tclaude_auto_rc_source", "@tclaude_path", "@tclaude_auto_rc_transcript")
    FORMAT = "\t".join("#{" + k + "}" for k in FIELDS)

    def __init__(self, cfg):
        self.cfg = cfg
        self.pending = set()
        self.next_scan = 0
        self.root = claude_root(cfg["home"])

    def tmux(self, *args):
        return subprocess.run([self.cfg["tmux"], "-u", *args], capture_output=True, text=True, timeout=5)

    def eligible(self, fields, now):
        window, pane, pid, panes, viewers, token, started, sid, used, source, folder, transcript = fields
        if (not re.fullmatch(r"@[0-9]+", window) or not re.fullmatch(r"%[0-9]+", pane)
                or not pid.isdecimal() or panes != "1" or viewers != "0" or used
                or not UUID.fullmatch(token) or not UUID.fullmatch(sid)
                or not started.isdecimal() or not 0 < int(started) <= now
                or source not in ("startup", "resume")
                or not os.path.isabs(folder) or os.path.realpath(folder) != folder or not os.path.isdir(folder)):
            return None
        proc = idle_claude(self.root, int(pid), sid, folder, now)
        if proc is None:
            return None
        empty, exists, first_bridge = transcript_evidence(transcript, self.root, sid)
        # A resumed conversation without its transcript is not proof of emptiness.
        if not empty or (not exists and source != "startup"):
            return None
        old_process = proc[-1] and now - int(started) > RETIRE_AFTER
        if not old_process and (first_bridge is None or now - first_bridge <= RETIRE_AFTER):
            return None
        git, _ = git_dirs(folder)
        try:
            checkout = os.stat(git or folder)
        except OSError:
            return None
        return (*fields, *proc[:-1], checkout.st_dev, checkout.st_ino)

    def sweep(self, now=None):
        now = time.time() if now is None else now
        result = self.tmux("list-windows", "-a", "-F", self.FORMAT)
        if result.returncode:
            self.pending.clear()
            return
        current = set()
        for line in set(result.stdout.splitlines()):
            fields = line.split("\t")
            if len(fields) != len(self.FIELDS):
                continue
            # A prompt delivered after a crash between intent and close keeps
            # the window alive and cancels that launch's unfinished retirement.
            if fields[8] and UUID.fullmatch(fields[5]) and os.path.isabs(fields[-2]):
                try:
                    marker = retirement_marker(fields[-2], self.cfg["home"])
                    info = os.lstat(marker)
                    intent = read_intent(marker)
                    if intent is not None and intent["launch"] == fields[5]:
                        cancel_intent(marker, info)
                except (OSError, ValueError):
                    pass
                continue
            proof = self.eligible(fields, now)
            if proof is None:
                continue
            current.add(proof)
            if proof in self.pending:
                try:
                    if self.retire(proof, now):
                        current.remove(proof)
                except (OSError, subprocess.SubprocessError, ValueError):
                    log_rarely("retire-window", "could not confirm an unused window's retirement")
        self.pending = current

    def tick(self):
        now = time.monotonic()
        if now >= self.next_scan:
            self.next_scan = now + RETIRE_SCAN
            self.sweep()

    def retire(self, proof, now):
        socket = self.tmux("display-message", "-p", "#{socket_path}")
        if socket.returncode:
            return False
        sock = os.path.realpath(socket.stdout.removesuffix("\n"))
        parent = os.path.dirname(sock)
        st = os.stat(parent)
        if not stat.S_ISSOCK(os.stat(sock).st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            return False  # custom, shared sockets do not use this lock location
        fd = os.open(sock + ".tclaude-launch.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            lock = os.fstat(fd)
            if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid():
                return False
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return self.retire_locked(proof, now)
        finally:
            os.close(fd)

    def retire_locked(self, proof, now):
        window, pane = proof[:2]
        snapshot = self.tmux("display-message", "-p", "-t", pane, self.FORMAT)
        fields = snapshot.stdout.removesuffix("\n").split("\t")
        if snapshot.returncode or len(fields) != len(self.FIELDS) or self.eligible(fields, now) != proof:
            return False
        folder = fields[-2]
        marker = retirement_marker(folder, self.cfg["home"])
        intent = retirement_intent(marker, fields[5], fields[7], fields[9], fields[-1])
        if intent is None:
            return False
        cancel = False
        try:
            # Recheck after syncing the marker. Prompt hooks can mark the window used
            # without the launch lock; evaluate that and viewer state in the kill command.
            if self.eligible(fields, now) != proof:
                cancel = True
                return False
            checks = (("window_id", window), ("pane_id", pane), ("pane_pid", fields[2]),
                      ("window_panes", "1"), ("window_active_clients", "0"),
                      ("@tclaude_auto_rc", fields[5]), ("@tclaude_auto_rc_sid", fields[7]),
                      ("@tclaude_auto_rc_used", ""))
            condition = "1"
            for name, value in checks:
                condition = "#{&&:" + condition + ",#{==:#{" + name + "}," + value + "}}"
            result = self.tmux("if-shell", "-F", "-t", pane, condition,
                               f"kill-window -t {window}", "display-message -p kept")
            cancel = result.returncode == 0 and result.stdout.strip() == "kept"
            # tmux may close the window before its client loses the reply. Keep
            # the intent unless we know that the close did not happen.
            closed = result.returncode == 0 and not cancel
            if closed:
                if os.path.samestat(os.lstat(marker), intent):
                    write_marker(marker, b"retired\n", replace=True)
                log("retired unused Claude Remote Control window in %s" % folder)
            return closed
        finally:
            if cancel:
                cancel_intent(marker, intent)


# ------------------------------------------------------------------ state

def current_boot_id():
    """Changes on every boot of the host, never on a restart of this process."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class State:
    """known: path -> {"claude": bool, "codex": bool, "tried": {"claude": ts, "codex": ts}}

    The boot id is kept too. tmux windows do not survive a reboot but this file does, so a Claude session
    this watcher launched is marked done for a host that no longer has it; `rebooted` says the host has
    booted since the state was written.
    """

    def __init__(self, path, boot_id=None):
        self.path = path
        self.first_run = not os.path.exists(path)
        self.known = {}
        # True once the state was written by a version that launches every clone. An older file lacks it:
        # its first scan here records the unmarked checkouts already present instead of launching them.
        self.clones = self.first_run
        # After a reboot: the active folders still to launch, and those launched this boot (trust is kept for them).
        self.boot = {"pending": [], "launched": [], "tried": {}}
        self.boot_id = current_boot_id() if boot_id is None else boot_id
        saved = None
        if not self.first_run:
            try:
                with open(path) as fh:
                    data = json.load(fh)
                self.known = data.get("known", {})
                self.boot.update(data.get("boot") or {})
                saved = data.get("boot_id")
                self.clones = bool(data.get("clones"))
            except (OSError, ValueError):
                self.known = {}
        # An older state file has no boot id: not evidence of a reboot.
        self.rebooted = bool(saved) and bool(self.boot_id) and saved != self.boot_id

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".known.")
        with os.fdopen(fd, "w") as fh:
            json.dump({"known": self.known, "boot_id": self.boot_id, "boot": self.boot, "clones": self.clones},
                      fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


# ------------------------------------------------------------------ the scan

def scan_once(cfg, state, launcher, now=None):
    """One pass. Returns the repos a launch was attempted for."""
    now = time.time() if now is None else now
    found = list(candidates(cfg["roots"], cfg.get("require_clone_marker", False)))
    present = set(found)
    attempted = []

    # Recording only, not launching: the first run, or the first scan after upgrading to "every clone counts".
    adopting = state.first_run or not state.clones

    # A path that is no longer a candidate is forgotten, so deleting and re-cloning is new again.
    for path in [p for p in state.known if p not in present]:
        del state.known[path]

    owners_changed = refresh_owners(cfg)
    owners, repos = cfg["owners"], cfg["repos"]

    # The host rebooted since the state was written: its tmux windows are gone. Start Claude in every folder
    # with session activity in the last 30 days (agent-active-dirs: prompted or marked), the boot launchers'
    # own rule, so a folder nobody prompted stays closed. Done once per boot; a window the user closes
    # afterwards stays closed. Codex threads persist in Codex itself, so they are left alone.
    if state.rebooted:
        active = [p for p in cfg["active_dirs"](cfg) if is_allowed(p, owners, repos)]
        state.boot = {"pending": active, "launched": [], "tried": {}}
        log("host rebooted: starting Claude in %d folders with session activity in the last 30 days" % len(active))
        state.rebooted = False
    for path in list(state.boot["pending"]):
        if claude_retired(path, cfg["home"]):
            state.boot["pending"].remove(path)
            state.boot.setdefault("tried", {}).pop(path, None)
            continue
        tried = state.boot.setdefault("tried", {})
        if path in tried and now - tried[path] < RETRY_SECONDS:
            continue
        tried[path] = now
        ok = launcher.start("claude", path)
        if ok:
            state.boot["pending"].remove(path)
            state.boot["launched"].append(path)
            attempted.append((path, "claude", ok))
        elif getattr(launcher, "cheap_retry", {}).get("claude"):
            tried.pop(path, None)             # no tmux server yet: cheap to look again next tick

    for path in found:
        entry = state.known.get(path)
        ident = None
        if entry is not None:
            # The same PATH is not the same checkout: a clone deleted and replaced between two ticks keeps its
            # pathname. Compare the identity recorded when it was first seen.
            ident = repo_identity(path)
            if entry.get("id") is None:
                entry["id"] = ident           # recorded by an older version
            elif ident is not None and entry["id"] != ident:
                log("checkout replaced at %s; treating it as new" % path)
                del state.known[path]
                entry = None
        if entry is not None and entry.get("ignored"):
            # Not this account's: remembered, so adding an owner later does not launch every old clone. A repo
            # that appeared AFTER the first run is looked at again when the allowed owners change.
            if not (owners_changed and not entry.get("seeded") and is_allowed(path, owners, repos) and is_finished(path)):
                continue
            entry = state.known[path] = {"claude": False, "codex": False, "tried": {}, "id": entry.get("id")}
            log("new repository (its owner is allowed now): %s" % path)
        if entry is None:
            ident = repo_identity(path)
            if not is_finished(path):
                continue                      # a clone still running: looked at again next tick
            if not is_allowed(path, owners, repos):
                state.known[path] = {"ignored": True, "seeded": adopting, "id": ident}
                continue
            if adopting:
                state.known[path] = {"claude": True, "codex": True, "seeded": True, "id": ident}
                continue
            entry = state.known[path] = {"claude": False, "codex": False, "tried": {}, "id": ident}
            log("new repository: %s" % path)
        if entry.get("claude") and entry.get("codex"):
            continue
        for kind in ("claude", "codex"):
            if entry.get(kind):
                continue
            if kind == "claude" and claude_retired(path, cfg["home"]):
                entry[kind] = True
                continue
            tried = entry.setdefault("tried", {})
            if now - tried.get(kind, 0) < RETRY_SECONDS and kind in tried:
                continue
            tried[kind] = now
            ok = launcher.start(kind, path)
            if ok is None:
                tried.pop(kind, None)         # still running (a Codex seed turn takes minutes): ask again next tick
                continue
            attempted.append((path, kind, ok))
            if ok:
                entry[kind] = True
            else:
                log("%s for %s is not ready; will retry" % (kind, path))
                # a failed attempt waits RETRY_SECONDS, except "no tmux server yet" (cheap to poll)
                if getattr(launcher, "cheap_retry", {}).get(kind):
                    tried.pop(kind, None)
    if not state.first_run and not state.clones:
        log("adopted the checkouts already present; from now on every new clone gets a session")
    state.first_run = False
    state.clones = True
    keep_trust(cfg["home"], [p for p, e in state.known.items() if e.get("claude") and not e.get("seeded")]
               + list(state.boot.get("launched", [])), state)
    state.save()
    return attempted


# ------------------------------------------------------------------ the real launcher

def cksum(text):
    out = subprocess.run(["cksum"], input=text.encode(), capture_output=True, check=True).stdout.split()
    return out[0].decode()


def trust_repos(home, repos):
    """Pre-accept Claude Code's per-directory trust prompt for repos that passed the gates above, in ONE
    read and at most one write. Returns how many were added."""
    path = os.path.join(home, ".claude.json")
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return 0
    added = 0
    for repo in repos:
        entry = cfg.setdefault("projects", {}).setdefault(repo, {})
        if entry.get("hasTrustDialogAccepted") is not True:
            entry["hasTrustDialogAccepted"] = True
            added += 1
    if not added:
        return 0
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".claude.json.")
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return added


def trust_repo(home, repo):
    trust_repos(home, [repo])


def keep_trust(home, repos, state):
    """A running Claude rewrites ~/.claude.json from its own cached copy, which silently drops a trust entry
    added meanwhile (the boot launcher documents this: claude-remote-control.tf). The watcher necessarily adds
    trust while sessions are live, so it also keeps it: when the file has changed it checks every repo it has
    launched and puts back any entry that went missing. The common tick is one stat."""
    if not repos:
        return
    path = os.path.join(home, ".claude.json")
    try:
        st = os.stat(path)
    except OSError:
        return
    sig = (st.st_mtime_ns, st.st_size, tuple(sorted(repos)))
    if getattr(state, "trust_sig", None) == sig:
        return
    added = trust_repos(home, repos)
    if added:
        log("restored Claude trust for %d repositories (another Claude session overwrote ~/.claude.json)" % added)
        st = os.stat(path)
    state.trust_sig = (st.st_mtime_ns, st.st_size, tuple(sorted(repos)))


class Launcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.children = {}                    # path -> the running codex seed
        self.cheap_retry = {"claude": True}   # no tmux server yet: look again next tick

    def tmux_up(self):
        r = subprocess.run([self.cfg["tmux"], "list-sessions"], capture_output=True, text=True)
        return r.returncode == 0

    def start(self, kind, path):
        return self.start_claude(path) if kind == "claude" else self.start_codex(path)

    def claude_logged_in(self):
        claude = os.path.join(self.cfg["home"], ".local", "bin", "claude")
        r = subprocess.run([claude, "auth", "status"], capture_output=True, timeout=30,
                           env=dict(os.environ, HOME=self.cfg["home"]))
        return r.returncode == 0

    def start_claude(self, path):
        if claude_retired(path, self.cfg["home"]):
            return True                        # handled, not a retry
        # Looked for on EVERY attempt, never cached: on a rebuilt box this watcher can start before the
        # installer has put t-claude in place, and "not there yet" must never read as "nothing to do".
        t_claude = find_t_claude(self.cfg["home"], self.cfg.get("t_claude_arg"))
        if not t_claude:
            log_rarely("no-t-claude", "t-claude is not installed yet; will keep looking")
            return False
        if not self.tmux_up():
            return False                      # never own the user's tmux server from here
        if not self.claude_logged_in():
            log_rarely("no-claude-login", "claude is not logged in yet; will keep looking")
            return False                      # a window with no login only sits at the login screen
        trust_repo(self.cfg["home"], path)
        script = 'source "$TCLAUDE" || exit 1; cd -- "$1" || exit 1; t-claude --auto --remote-control'
        env = dict(os.environ, HOME=self.cfg["home"], TCLAUDE=t_claude, TERM="xterm-256color",
                   TCLAUDE_RC_GATE=os.path.abspath(__file__))
        subprocess.run(["zsh", "-c", script, "agent-session-sync", path], env=env, timeout=120,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if claude_retired(path, self.cfg["home"]):
            return True                        # it retired while the shell was taking its launch lock
        key = "%s_%s" % (cksum(path), cksum(""))
        for _ in range(40):                   # up to ~10 s for the --remote-control child to appear
            out = subprocess.run([self.cfg["tmux"], "list-windows", "-a", "-F", "#{window_id} #{@tclaude_key}"],
                                 capture_output=True, text=True).stdout
            win = next((l.split()[0] for l in out.splitlines() if l.split()[1:] == [key]), None)
            if win:
                pane = subprocess.run([self.cfg["tmux"], "display-message", "-p", "-t", win, "#{pane_pid}"],
                                      capture_output=True, text=True).stdout.strip()
                kids = subprocess.run(["ps", "-o", "args=", "--ppid", pane], capture_output=True, text=True).stdout
                if "--remote-control" in kids and re.search(r"claude|nosync-wrap", kids):
                    log("claude live in %s" % path)
                    return True
            time.sleep(0.25)
        log("no live --remote-control process in %s" % path)
        return False

    def start_codex(self, path):
        """True when the seed finished successfully, None while it is running, False when it failed (retried
        after RETRY_SECONDS). A seed turn can take minutes and the child can fail (the Codex daemon starting or
        refusing), so starting it is not success: only its exit status is."""
        seed = self.cfg.get("codex_seed")
        if not seed or not os.access(seed, os.X_OK):
            # Not installed (yet): a retry, never success. Returning True here once left every new checkout on
            # a box without the helper permanently recorded as having a Codex thread it never got.
            log_rarely("no-codex-seeder", "%s is not installed; no Codex thread can be made yet" % (seed or "the Codex seeder"))
            return False
        child = self.children.get(path)
        if child is not None:
            rc = child.poll()
            if rc is None:
                return None
            del self.children[path]
            if rc == 0:
                log("codex thread seeded for %s" % path)
                return True
            log("codex seeding for %s exited %d" % (path, rc))
            return False
        codex = os.path.join(self.cfg["home"], ".local", "bin", "codex")
        if subprocess.run([codex, "login", "status"], capture_output=True, timeout=30).returncode != 0:
            return False                      # not logged in yet: retried later, Claude is not held up
        logdir = os.path.join(self.cfg["state_dir"])
        os.makedirs(logdir, exist_ok=True)
        with open(os.path.join(logdir, "codex-seed.log"), "ab") as fh:
            self.children[path] = subprocess.Popen([seed, path], stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                                                   start_new_session=True, env=dict(os.environ, HOME=self.cfg["home"]))
        log("codex seeding started for %s" % path)
        return None


# ------------------------------------------------------------------ main

def find_t_claude(home, given):
    """--t-claude PATH, or auto: the system copy (the metal boxes) else the user's own, else none."""
    if given and given != "auto":
        return given
    for path in ("/usr/local/lib/fcvm/t-claude.zsh", os.path.join(home, ".config", "t-claude.zsh")):
        if os.path.isfile(path):
            return path
    return None


def build_config(args):
    home = args.home or os.path.expanduser("~")
    roots = args.root or [os.path.join(home, "*"), os.path.join(home, "src", "*")]
    state_dir = args.state_dir or os.path.join(home, ".local", "state", "agent-session-sync")
    return {
        "home": home, "roots": roots, "state_dir": state_dir,
        "require_clone_marker": args.require_clone_marker,
        "owners": allowed_owners(home, args.owner or []), "owners_extra": list(args.owner or []),
        "owners_sig": owners_signature(home), "repos": set(args.repo or []),
        "tmux": args.tmux, "t_claude_arg": args.t_claude, "codex_seed": args.codex_seed,
        "active_dirs": lambda cfg, tool=args.active_dirs: run_active_dirs(tool, cfg["home"]),
    }


def run_active_dirs(tool, home):
    """The folders agent-active-dirs names for this account, or none when it cannot run."""
    try:
        r = subprocess.run([tool, "--home", home], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        log("agent-active-dirs did not run: %s" % exc)
        return []
    if r.returncode != 0:
        log("agent-active-dirs failed: %s" % r.stderr.strip()[-300:])
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def file_version(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home")
    ap.add_argument("--root", action="append", help="glob of checkout directories (default ~/* and ~/src/*)")
    ap.add_argument("--owner", action="append", help="allowed GitHub owner (also: the account's own gh login)")
    ap.add_argument("--repo", action="append", help="one allowed OWNER/REPO, exactly")
    ap.add_argument("--require-clone-marker", action="store_true",
                    help="require claude-interactive for main checkouts too (administration jumpboxes)")
    ap.add_argument("--t-claude", default="auto", help="t-claude.zsh to source (default: the system copy, else ~/.config/t-claude.zsh)")
    ap.add_argument("--codex-seed", default="/usr/local/bin/codex-seed-thread")
    ap.add_argument("--active-dirs", default="/usr/local/bin/agent-active-dirs")
    ap.add_argument("--tmux", default="tmux")
    ap.add_argument("--state-dir")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--retire-claude-only", action="store_true",
                    help="retire unused Claude windows without discovering or starting repositories")
    ap.add_argument("--check-claude-rc", metavar="PATH",
                    help="exit nonzero if automatic Claude Remote Control is retired for this checkout")
    args = ap.parse_args(argv)
    home = args.home or os.path.expanduser("~")
    if args.check_claude_rc is not None:
        return int(claude_retired(args.check_claude_rc, home))
    state = None
    if args.retire_claude_only:
        cfg = {"home": home, "tmux": args.tmux}
    else:
        cfg = build_config(args)
        state = State(os.path.join(cfg["state_dir"], "known.json"))
        launcher = Launcher(cfg)
        log("watching %s as %s; owners=%s repos=%s first_run=%s" % (
            cfg["roots"], cfg["home"], sorted(cfg["owners"]), sorted(cfg["repos"]), state.first_run))
    retirement = ClaudeRetirement(cfg)
    me = os.path.abspath(__file__)
    version = file_version(me)
    while True:
        if state is not None:
            try:
                scan_once(cfg, state, launcher)
            except Exception as exc:              # a bad tick must never end the watch
                log("scan failed: %s" % exc)
        try:
            retirement.tick()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log_rarely("retirement", "Claude retirement check failed: %s" % type(exc).__name__)
        if args.once:
            return 0
        time.sleep(args.interval)
        if version is not None and file_version(me) not in (None, version):
            # An update replaced this file. Run the new code, in this same process and unit: nothing of the
            # sessions is touched, and the state is on disk. (A box's installer never has to restart it.)
            log("my code changed; running the new version")
            os.execv(sys.executable, [sys.executable, me] + list(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""agent-session-sync -- make a NEW repository show up in Claude Code and Codex within seconds.

Installed as /usr/local/bin/agent-session-sync and run as a small service (agent-session-sync.service,
or agent-session-sync@<user>.service on nextjs-dev) on every box that keeps remote-control sessions.
It replaces "the boot launcher looked once, at boot" with a cheap watch loop (default every 5 s).

WHAT COUNTS AS NEW. A top-level checkout, directly under one of the roots (default ~/* and ~/src/*),
that appeared after the watcher first looked:
  * a MAIN checkout only: `.git` is a DIRECTORY. A linked `git worktree` has `.git` as a FILE, so a
    random worktree is never new, and nothing nested below a root is ever scanned;
  * finished: HEAD resolves, no *.lock in .git (a clone, fetch or checkout is still running) and the
    index exists (a clone writes it last), so a half-cloned repo is not launched;
  * allowed: its origin is owned by an allowed owner (the account's own GitHub login, plus
    ~/.config/agent-session-sync/owners and --owner) or is an exact --repo, and it is not claude-code-sync.
The FIRST run only records what already exists and launches nothing: the boot launcher decided about
those (a clone idle for 30 days was skipped on purpose and must stay skipped).

WHAT IT DOES for a new repo: pre-accept Claude's per-folder trust prompt (only for a repo that passed
the gates above), start `t-claude --auto --remote-control` there (a window in the user's EXISTING tmux
server; with no server yet it waits rather than own one), and seed a Codex thread (detached: a seed
turn can take minutes, the thread itself exists at once). Claude and Codex are tracked separately, so
a Codex that is not logged in yet does not hold up Claude and is retried.

COST. Per tick: one listing and one stat per entry in the roots. Anything already handled is skipped
before any deeper check, so an idle box does almost nothing.
"""
import argparse
import glob
import json
import os
import re
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


def is_main_checkout(path):
    """`.git` is a directory. A linked worktree (or a submodule) has a `.git` FILE."""
    git = os.path.join(path, ".git")
    return os.path.isdir(git) and not os.path.islink(git)


def is_finished(path):
    git = os.path.join(path, ".git")
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
    origin = parse_origin(read_origin(os.path.join(path, ".git")))
    if not origin:
        return False
    owner, repo = origin
    if repo in NEVER_REPOS:
        return False
    return owner.lower() in owners or "%s/%s" % (owner, repo) in repos


def candidates(roots):
    """Directories directly under the roots whose .git is a directory."""
    seen = set()
    for pattern in roots:
        for p in glob.glob(pattern):
            name = os.path.basename(p)
            if name.startswith("."):
                continue
            real = os.path.realpath(p)
            if real in seen or not is_main_checkout(real):
                continue
            seen.add(real)
            yield real


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
    try:
        st = os.stat(os.path.join(path, ".git"))
    except OSError:
        return None
    return [st.st_dev, st.st_ino]


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
        self.boot_id = current_boot_id() if boot_id is None else boot_id
        saved = None
        if not self.first_run:
            try:
                with open(path) as fh:
                    data = json.load(fh)
                self.known = data.get("known", {})
                saved = data.get("boot_id")
            except (OSError, ValueError):
                self.known = {}
        # An older state file has no boot id: not evidence of a reboot.
        self.rebooted = bool(saved) and bool(self.boot_id) and saved != self.boot_id

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".known.")
        with os.fdopen(fd, "w") as fh:
            json.dump({"known": self.known, "boot_id": self.boot_id}, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


# ------------------------------------------------------------------ the scan

def scan_once(cfg, state, launcher, now=None):
    """One pass. Returns the repos a launch was attempted for."""
    now = time.time() if now is None else now
    found = list(candidates(cfg["roots"]))
    present = set(found)
    attempted = []

    # A path that is no longer a main checkout is forgotten, so deleting and re-cloning is new again.
    for path in [p for p in state.known if p not in present]:
        del state.known[path]

    # The host rebooted since the state was written: its tmux windows are gone, so every Claude session THIS
    # WATCHER launched is to be launched again. (Repos recorded at the first run were never ours to launch:
    # the boot launcher owns those. Codex threads persist in Codex itself, so they are left alone.) Done
    # once; a window the user closes afterwards stays closed.
    if state.rebooted:
        again = [p for p, e in state.known.items() if e.get("claude") and not e.get("seeded") and not e.get("ignored")]
        for p in again:
            state.known[p]["claude"] = False
            state.known[p].setdefault("tried", {}).pop("claude", None)
        if again:
            log("host rebooted: starting Claude again in %d repositories" % len(again))
        state.rebooted = False

    owners_changed = refresh_owners(cfg)
    owners, repos = cfg["owners"], cfg["repos"]

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
            if not is_allowed(path, owners, repos):
                state.known[path] = {"ignored": True, "seeded": state.first_run, "id": ident}
                continue
            if not is_finished(path):
                continue                      # a clone still running: looked at again next tick
            if state.first_run:
                state.known[path] = {"claude": True, "codex": True, "seeded": True, "id": ident}
                continue
            entry = state.known[path] = {"claude": False, "codex": False, "tried": {}, "id": ident}
            log("new repository: %s" % path)
        if entry.get("claude") and entry.get("codex"):
            continue
        for kind in ("claude", "codex"):
            if entry.get(kind):
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
    state.first_run = False
    keep_trust(cfg["home"], [p for p, e in state.known.items() if e.get("claude") and not e.get("seeded")], state)
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
        env = dict(os.environ, HOME=self.cfg["home"], TCLAUDE=t_claude, TERM="xterm-256color")
        subprocess.run(["zsh", "-c", script, "agent-session-sync", path], env=env, timeout=120,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            return True                       # no Codex seeding on this account
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
        "owners": allowed_owners(home, args.owner or []), "owners_extra": list(args.owner or []),
        "owners_sig": owners_signature(home), "repos": set(args.repo or []),
        "tmux": args.tmux, "t_claude_arg": args.t_claude, "codex_seed": args.codex_seed,
    }


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
    ap.add_argument("--t-claude", default="auto", help="t-claude.zsh to source (default: the system copy, else ~/.config/t-claude.zsh)")
    ap.add_argument("--codex-seed", default="/usr/local/bin/codex-seed-thread")
    ap.add_argument("--tmux", default="tmux")
    ap.add_argument("--state-dir")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    cfg = build_config(args)
    state = State(os.path.join(cfg["state_dir"], "known.json"))
    launcher = Launcher(cfg)
    log("watching %s as %s; owners=%s repos=%s first_run=%s" % (
        cfg["roots"], cfg["home"], sorted(cfg["owners"]), sorted(cfg["repos"]), state.first_run))
    me = os.path.abspath(__file__)
    version = file_version(me)
    while True:
        try:
            scan_once(cfg, state, launcher)
        except Exception as exc:              # a bad tick must never end the watch
            log("scan failed: %s" % exc)
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

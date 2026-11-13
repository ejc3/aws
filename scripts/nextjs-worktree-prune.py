#!/usr/bin/env python3
"""Remove clean, proven merged linked worktrees when this account's disk is full."""
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import quote


SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
TOKEN = re.compile(r"[0-9a-f]{64}")
ACCOUNT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
GITHUB = re.compile(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")
PUBLISHED = Path("/var/lib/ndev/instances")
PROC = Path("/proc")
MARKER_AGE = 30 * 86400
TIMEOUT = 10
OUTPUT_LIMIT = 2 * 1024**2
LIST_LIMIT = 512
COMMAND_OWNER = None


class Incomplete(Exception):
    """The account cannot be ranked completely during this run."""


def pressure(home):
    usage = os.statvfs(home)
    return (usage.f_bavail * usage.f_frsize < 20 * 1024**3
            or (usage.f_blocks and usage.f_bavail * 10 <= usage.f_blocks)
            or (usage.f_files and usage.f_favail * 10 <= usage.f_files))


def under(path, parent):
    return path == parent or parent in path.parents


def overlaps(a, b):
    return under(a, b) or under(b, a)


def owned(path, uid, device):
    """Do not follow a checkout through symlinks or onto another filesystem."""
    try:
        info = path.lstat()
        return (stat.S_ISDIR(info.st_mode) and info.st_uid == uid
                and info.st_dev == device and path.resolve(strict=True) == path)
    except (OSError, RuntimeError):
        return False


def run(home, argv, timeout=TIMEOUT):
    # Git and gh may use the account's own credentials, never inherited AWS or Git
    # routing variables. Do not put repository stderr, URLs or API bodies in logs.
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
        "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
    }
    credentials = {}
    if os.geteuid() == 0:
        if COMMAND_OWNER is None:
            raise Incomplete("refusing to run repository commands as root")
        uid, gid = COMMAND_OWNER
        if uid <= 0 or gid <= 0:
            raise Incomplete("invalid command owner")
        credentials = {"user": uid, "group": gid, "extra_groups": []}
    try:
        p = subprocess.Popen(argv, cwd=home, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                             **credentials)
    except OSError:
        return None
    out = bytearray()
    end = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(p.stdout, selectors.EVENT_READ)
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not poll.select(remaining):
                    raise Incomplete("command timed out")
                data = os.read(p.stdout.fileno(), min(65536, OUTPUT_LIMIT - len(out) + 1))
                if not data:
                    break
                if len(data) + len(out) > OUTPUT_LIMIT:
                    raise Incomplete("command output exceeded limit")
                out.extend(data)
        try:
            status = p.wait(timeout=max(0, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise Incomplete("command timed out") from None
        return bytes(out) if status == 0 else None
    except BaseException:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        raise
    finally:
        p.stdout.close()


def git(home, repo, *args, timeout=TIMEOUT):
    return run(home, ["/usr/bin/git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
                      "-c", "core.fsmonitor=false", "-c", "core.ignoreStat=false", *args], timeout)


def roots(home, uid, device):
    # Match how interactive checkouts are laid out, plus the usual projects folder.
    for directory in (home, home / "src", home / "projects"):
        if not owned(directory, uid, device):
            continue
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index == 2048:
                    raise Incomplete("repository discovery exceeded limit")
                root = Path(entry.path)
                if entry.is_dir(follow_symlinks=False) and owned(root, uid, device):
                    if owned(root / ".git", uid, device):
                        yield root


def worktrees(home, repo):
    raw = git(home, repo, "worktree", "list", "--porcelain", "-z")
    if raw is None:
        raise Incomplete("unable to enumerate linked worktrees")
    records = [dict(line.partition(" ")[::2] for line in os.fsdecode(row).split("\0"))
               for row in raw.rstrip(b"\0").split(b"\0\0") if row]
    # The first record is the primary checkout. Refuse an unexpected primary.
    if not records or records[0].get("worktree") != str(repo):
        raise Incomplete("unexpected primary checkout")
    if any("worktree" not in record for record in records):
        raise Incomplete("invalid linked worktree metadata")
    return records[1:]


def github(home, endpoint):
    raw = run(home, ["/usr/bin/gh", "api", "--hostname", "github.com", "--method", "GET", endpoint])
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def remote_default(home, repo, origin):
    # The origin is an exact GitHub repository slug. Only the account's GitHub
    # CLI can make network calls; Git cannot open SSH or run a remote helper.
    info = github(home, f"repos/{origin}")
    if not isinstance(info, dict) or str(info.get("full_name", "")).lower() != origin.lower():
        return None
    branch = info.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > 512:
        return None
    ref = "refs/heads/" + branch
    if git(home, repo, "check-ref-format", ref) is None:
        return None
    row = github(home, f"repos/{origin}/git/ref/heads/{quote(branch, safe='')}")
    if isinstance(row, dict) and row.get("ref") == ref and isinstance(row.get("object"), dict):
        obj = row["object"]
        sha = obj.get("sha")
        if obj.get("type") == "commit" and isinstance(sha, str) and SHA.fullmatch(sha):
            return ref, sha
    return None


def ancestor(home, repo, older, newer, origin):
    if git(home, repo, "merge-base", "--is-ancestor", older, newer) is not None:
        return True
    if all(git(home, repo, "cat-file", "-e", rev + "^{commit}") is not None for rev in (older, newer)):
        return False
    # A clone may not have downloaded the new default or squash commit. Use
    # ancestry metadata only, without fetching objects onto a full disk.
    row = github(home, f"repos/{origin}/compare/{older}...{newer}?per_page=1&page=2")
    return (isinstance(row, dict) and row.get("status") in {"ahead", "identical"}
            and type(row.get("behind_by")) is int and row["behind_by"] == 0
            and all(isinstance(row.get(key), dict) and row[key].get("sha") == older
                    for key in ("base_commit", "merge_base_commit")))


def merged(home, repo, head, ref, tip, origin):
    if ancestor(home, repo, head, tip, origin):
        return True
    # A squash merge need not retain the original commit as an ancestor. Require
    # GitHub's exact PR head and a merge commit still on the verified default.
    pulls = github(home, f"repos/{origin}/commits/{head}/pulls?per_page=30")
    if not isinstance(pulls, list):
        return False
    try:
        for pull in pulls:
            commit = pull.get("merge_commit_sha") or ""
            if (pull.get("merged_at") and pull["head"]["sha"] == head
                    and pull["base"]["ref"] == ref.removeprefix("refs/heads/")
                    and pull["base"]["repo"]["full_name"].lower() == origin.lower()
                    and SHA.fullmatch(commit) and ancestor(home, repo, commit, tip, origin)):
                return True
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
    return False


def protected_paths(home, uid, inspect_all=False):
    """Protect kernel paths and ndev registrations; production inspects every UID."""
    reserved = set()
    active = set()
    # Mounts can share st_dev (bind mounts); protect the mountpoint itself as
    # well as parents that Git would otherwise recurse into during removal.
    for line in (PROC / "self/mountinfo").read_text().splitlines():
        name = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
        path = Path(name)
        if path != home and under(path, home):
            reserved.add(path)

    with os.scandir(PROC) as processes:
        for entry in processes:
            if not entry.name.isdecimal() or int(entry.name) == os.getpid():
                continue
            proc = Path(entry.path)
            try:
                if not inspect_all:
                    with (proc / "status").open() as status:
                        owner = next((line.split()[1:3] for line in status if line.startswith("Uid:")), [])
                    if str(uid) not in owner:
                        continue
                with os.scandir(proc / "fd") as descriptors:
                    for link in itertools.chain((proc / "cwd",), (Path(e.path) for e in descriptors)):
                        try:
                            target = Path(os.readlink(link).removesuffix(" (deleted)"))
                        except FileNotFoundError:
                            continue
                        if target.is_absolute() and under(target, home):
                            active.add(target)
            except (FileNotFoundError, ProcessLookupError):
                continue  # The process or descriptor exited during inspection.

    # Keep published worktrees even when the site's service is currently idle.
    try:
        if PUBLISHED.resolve(strict=True) != PUBLISHED:
            raise OSError("unsafe publishing directory")
        with os.scandir(PUBLISHED) as entries:
            for entry in entries:
                if not entry.name.endswith(".env"):
                    continue
                _, text = bounded_file(Path(entry.path), 65536)
                directory = [line[4:] for line in text.splitlines()
                             if line.startswith("DIR=")]
                if len(directory) != 1 or not Path(directory[0]).is_absolute():
                    raise OSError("invalid publishing record")
                path = Path(directory[0]).resolve()
                if under(path, home):
                    reserved.add(path)
    except FileNotFoundError:
        if PUBLISHED.exists() or PUBLISHED.is_symlink():
            raise
    return active, reserved


def protected(path, paths):
    active, reserved = paths
    return any(under(p, path) for p in active) or any(overlaps(p, path) for p in reserved)


def bounded_file(path, limit, uid=None, device=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or (uid is not None and info.st_uid != uid)
                or (device is not None and info.st_dev != device) or info.st_size > limit):
            raise OSError("invalid metadata file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise OSError("metadata exceeded limit")
    return info, os.fsdecode(data).rstrip("\n")


def identity(repo, path, head, uid, device):
    """Read the registered gitfile directly for cheap, stable re-identification."""
    gitfile = path / ".git"
    try:
        if not owned(path, uid, device):
            return None
        info, content = bounded_file(gitfile, 8192, uid, device)
        if not content.startswith("gitdir: ") or "\n" in content:
            return None
        admin = (path / content[8:]).resolve(strict=True)
        if admin.parent != repo / ".git/worktrees" or not owned(admin, uid, device):
            return None
        _, backref = bounded_file(admin / "gitdir", 8192, uid, device)
        if (admin / backref).resolve(strict=True) != gitfile:
            return None
        root_stat, admin_stat = path.stat(), admin.stat()
        dates = [root_stat.st_mtime_ns, info.st_mtime_ns]
        for file in (admin / "HEAD", admin / "logs/HEAD"):
            try:
                stamp = file.lstat()
            except FileNotFoundError:
                if file.name == "HEAD" and file.parent == admin:
                    return None
                continue
            if not stat.S_ISREG(stamp.st_mode) or stamp.st_uid != uid:
                return None
            dates.append(stamp.st_mtime_ns)
        age = max(dates)
        key = json.dumps([str(repo), str(path), head, age, root_stat.st_ino, info.st_ino, admin_stat.st_ino])
        return admin, age, hashlib.sha256(key.encode()).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def clean(home, path, head, directory):
    actual = git(home, path, "rev-parse", "--verify", "HEAD")
    if actual is None or os.fsdecode(actual).strip() != head:
        return False
    if not (directory / "index").is_file() or any(directory.glob("*.lock")):
        return False
    if any((directory / name).exists() for name in
           ("rebase-merge", "rebase-apply", "sequencer", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_START")):
        return False
    marker = directory / "claude-interactive"
    if marker.exists() and time.time() - marker.stat().st_mtime < MARKER_AGE:
        return False
    # A top-level entry absent from HEAD is already a reason to preserve the
    # checkout, including a staged addition. Read only the root tree so large
    # ignored dependency and build caches do not require a recursive scan.
    entries = git(home, path, "ls-tree", "-z", "HEAD")
    if entries is None:
        return False
    tracked_roots = {b".git"}
    for entry in entries.split(b"\0"):
        if entry:
            _, separator, name = entry.partition(b"\t")
            if not separator or not name:
                return False
            tracked_roots.add(name)
    try:
        with os.scandir(os.fsencode(path)) as checkout:
            if any(entry.name not in tracked_roots for entry in checkout):
                return False
    except OSError:
        return False
    if git(home, path, "status", "--porcelain=v1", "-z", "--untracked-files=normal",
           "--ignored=matching", "--ignore-submodules=none") != b"":
        return False
    # Git status can hide edits to assume-unchanged or skip-worktree files.
    index = git(home, path, "ls-files", "-v", "-z")
    return index is not None and all(e.startswith(b"H ") for e in index.split(b"\0") if e)


def local_repo_ok(home, repo):
    # Partial clones can lazily fetch missing objects under pressure.
    settings = git(home, repo, "config", "--null", "--list")
    return (settings is not None and not re.search(
        rb"(?:^|\0)(?:extensions\.partialclone|remote\.[^\n]*\.promisor)\n", settings.lower())
        and not (repo / ".git/info/grafts").exists())


def prune(home, uid, token=None, *, inspect_all=False):
    skipped = {"status": "skipped"} if token else {"candidates": []}
    healthy = {"status": "healthy"} if token else {"candidates": []}
    if not pressure(home):
        return healthy
    end = time.monotonic() + 240
    device = home.stat().st_dev
    if not owned(home, uid, device):
        raise OSError("unsafe home")
    directories = list(dict.fromkeys(roots(home, uid, device)))
    candidates = []
    registered = set(directories)
    for repo in directories:
        if time.monotonic() >= end:
            raise Incomplete("repository discovery timed out")
        for record in worktrees(home, repo):
            path = Path(record.get("worktree", ""))
            if path == home or not under(path, home):
                continue
            registered.add(path.resolve())
            if len(registered) > 4096:
                raise Incomplete("worktree discovery exceeded limit")
            head = record.get("HEAD", "")
            if not {"bare", "locked", "prunable"} & record.keys() and SHA.fullmatch(head):
                info = identity(repo, path, head, uid, device)
                if info and (not token or token == info[2]):
                    candidates.append((info[1], repo, path, record, info))
    if time.monotonic() >= end:
        raise Incomplete("worktree discovery timed out")
    if not candidates or (token and len(candidates) != 1):
        return skipped
    paths = protected_paths(home, uid, inspect_all)
    eligible = []
    remotes = {}
    valid_repos = {}
    for age, repo, path, record, info in sorted(candidates, key=lambda item: (item[0], item[2])):
        if time.monotonic() >= end:
            raise Incomplete("worktree ranking timed out")
        if not pressure(home):
            return healthy
        if protected(path, paths) or any(p != path and under(p, path) for p in registered):
            continue
        head = record["HEAD"]
        if repo not in valid_repos:
            valid_repos[repo] = local_repo_ok(home, repo)
        if not valid_repos[repo]:
            continue
        try:
            is_clean = clean(home, path, head, info[0])
        except Incomplete:
            # A bounded Git read did not prove this checkout clean. Keep this
            # tree, but continue checking independently verified candidates.
            continue
        if not is_clean:
            continue
        if repo not in remotes:
            url = git(home, repo, "remote", "get-url", "origin")
            match = GITHUB.fullmatch(os.fsdecode(url).strip()) if url else None
            origin = match[1] if match else None
            remotes[repo] = (remote_default(home, repo, origin) if origin else None, origin)
        default, origin = remotes[repo]
        if not default or record.get("branch") == default[0] or not merged(home, repo, head, *default, origin):
            continue
        if not token:
            eligible.append({"token": info[2], "age_ns": age})
            if len(eligible) > LIST_LIMIT:
                raise Incomplete("eligible worktrees exceeded list limit")
            continue
        # Recheck all non-Git protections, registration and worktree content just
        # before removal. Use Git's ordinary removal and keep every branch ref.
        records = worktrees(home, repo)
        if (record not in records or identity(repo, path, head, uid, device) != info
                or any(Path(x["worktree"]) != path and under(Path(x["worktree"]).resolve(), path)
                       for x in records if "worktree" in x)
                or not clean(home, path, head, info[0]) or protected(path, protected_paths(home, uid, inspect_all))):
            return skipped
        if not pressure(home):
            return healthy
        removed = git(home, repo, "worktree", "remove", "--", str(path), timeout=50) is not None
        return {"status": "removed" if removed else "declined"}
    if time.monotonic() >= end:
        raise Incomplete("worktree ranking timed out")
    return skipped if token else {"candidates": eligible}


def main():
    global COMMAND_OWNER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", metavar="ACCOUNT", help="inspect every process, run Git as this account")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list eligible candidates without removing anything")
    mode.add_argument("--remove", metavar="TOKEN", help="revalidate and remove one listed candidate")
    args = parser.parse_args()
    if args.remove and not TOKEN.fullmatch(args.remove):
        parser.error("invalid worktree token")
    root = os.geteuid() == 0
    if root != bool(args.owner) or (root and not ACCOUNT.fullmatch(args.owner)):
        parser.error("a valid --owner is required as root and permitted only as root")
    try:
        entry = pwd.getpwnam(args.owner) if root else pwd.getpwuid(os.geteuid())
        uid, home = entry.pw_uid, Path(entry.pw_dir)
        if home == Path("/home") or not under(home, Path("/home")):
            raise OSError("home must be under /home")
        if root:
            if (uid <= 0 or entry.pw_gid <= 0 or entry.pw_name != args.owner
                    or entry.pw_dir != "/home/" + args.owner):
                raise OSError("invalid owner")
            COMMAND_OWNER = (uid, entry.pw_gid)
        result = prune(home, uid, args.remove, inspect_all=root)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, Incomplete):
        if not args.remove:
            print("worktree-prune: unable to rank account completely", file=sys.stderr)
            return 1
        result = {"status": "skipped"}
    finally:
        COMMAND_OWNER = None
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

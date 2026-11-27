#!/usr/bin/env python3
"""Remove clean, proven merged linked worktrees when this account's disk is full.

On fcvm-metal-arm only, the host driver then asks for a second pass: the
ignored build output (node_modules, .next, target) of idle linked worktrees.

With --retire (fcvm-metal-arm only) a worktree may hold ignored files
(.scratch/, .env.local): they are packed into a verified archive under
~/.local/share/worktree-archive before the worktree is removed, and a worktree
whose HEAD is on a live remote branch and that Git has not used for
RETIRE_IDLE_SECONDS qualifies as well as a merged one.
"""
import argparse
import contextlib
import gzip
import hashlib
import io
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
import syslog
import tarfile
import time
import traceback
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
RANK_SECONDS = 240
# The ranking budget's clock; Git and gh timeouts use time.monotonic directly.
clock = time.monotonic
COMMAND_OWNER = None
OPERATIONS = ("rebase-merge", "rebase-apply", "sequencer", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
              "BISECT_START")
# The build-output pass (--builds, --clean-builds). Only these directory names,
# at most this many levels below a linked worktree's root, are ever removed.
BUILD_NAMES = frozenset({"node_modules", ".next", "target"})
BUILD_DEPTH = 3
IDLE_SECONDS = 7 * 86400
BUILD_SECONDS = 150
DELETE_SECONDS = 60
WALK_LIMIT = 1_000_000
CURSOR = re.compile(r"([0-9]{1,19}):([0-9a-f]{64})")
# The time of a reflog line: "<old> <new> <name> <email> <seconds> <zone>\t<message>".
REFLOG_TIME = re.compile(rb"> ([0-9]{1,12}) [+-][0-9]{4}\t")
# Retirement (--retire): archive ignored files, then remove. Build output is not
# archived. A worktree whose archive input would exceed ARCHIVE_LIMIT is kept.
RETIRE_IDLE_SECONDS = 14 * 86400
ARCHIVE_DIR = Path(".local/share/worktree-archive")
ARCHIVE_LIMIT = 1024**3
ARCHIVE_SPACE = 4 * 1024**3
ARCHIVE_KEEP_SECONDS = 90 * 86400
ARCHIVE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}-[0-9a-f]{8}-[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z\.tar\.gz")
ARCHIVE_SECONDS = 120
# File times are wall-clock times, so the idle cutoff is too.
wall = time.time


class Incomplete(Exception):
    """A fixed diagnostic for the account journal. Keep paths and command output out of these messages."""


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
    # A squash merge need not retain the original commit as an ancestor. Require a
    # merged PR whose final head is this commit or descends from it (the branch
    # moved on after this worktree's last commit), and a merge commit still on the
    # verified default: everything up to the PR's head is in that merge.
    pulls = github(home, f"repos/{origin}/commits/{head}/pulls?per_page=30")
    if not isinstance(pulls, list):
        return False
    try:
        for pull in pulls:
            commit = pull.get("merge_commit_sha") or ""
            pr_head = pull["head"]["sha"]
            if (pull.get("merged_at") and isinstance(pr_head, str) and SHA.fullmatch(pr_head)
                    and (pr_head == head or ancestor(home, repo, head, pr_head, origin))
                    and pull["base"]["ref"] == ref.removeprefix("refs/heads/")
                    and pull["base"]["repo"]["full_name"].lower() == origin.lower()
                    and SHA.fullmatch(commit) and ancestor(home, repo, commit, tip, origin)):
                return True
    except (KeyError, TypeError, AttributeError, ValueError):
        return False
    return False


def on_remote(home, repo, head, origin):
    """HEAD is reachable from a branch that exists on the GitHub origin now.

    Candidate branches come from the local remote-tracking refs that contain
    HEAD; each is confirmed against GitHub's current branch tip, so a branch
    deleted upstream (a closed PR) does not count.
    """
    refs = git(home, repo, "for-each-ref", "--contains", head, "--format=%(refname)", "refs/remotes/origin/")
    if refs is None:
        return False
    for line in os.fsdecode(refs).splitlines()[:20]:
        branch = line.removeprefix("refs/remotes/origin/")
        if not branch or branch == "HEAD" or line == branch or len(branch) > 512:
            continue
        if git(home, repo, "check-ref-format", "refs/heads/" + branch) is None:
            continue
        row = github(home, f"repos/{origin}/git/ref/heads/{quote(branch, safe='')}")
        if not (isinstance(row, dict) and row.get("ref") == "refs/heads/" + branch
                and isinstance(row.get("object"), dict) and row["object"].get("type") == "commit"):
            continue
        tip = row["object"].get("sha")
        if isinstance(tip, str) and SHA.fullmatch(tip) and ancestor(home, repo, head, tip, origin):
            return True
    return False


@contextlib.contextmanager
def as_owner():
    """Run file work as the account, not root: read only what it may read, create only its own files.

    A no-op when the worker is not root. Supplementary groups are narrowed to
    the account's group for the duration and restored afterwards.
    """
    if os.geteuid() != 0:
        yield
        return
    uid, gid = COMMAND_OWNER
    groups = os.getgroups()
    os.setgroups([gid])
    os.setegid(gid)
    os.seteuid(uid)
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(0)
        os.setgroups(groups)


def archive_dir_fd(home, uid, device, *parts):
    """Open ~/ARCHIVE_DIR[/parts], creating each level 0700, one component at a time by fd.

    No name is followed through a symlink. Call inside as_owner(), so a created
    directory belongs to the account with no chown.
    """
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in (*ARCHIVE_DIR.parts, *parts):
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fd)
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
            info = os.fstat(fd)
            if info.st_uid != uid or info.st_dev != device:
                raise Incomplete("unsafe archive directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def expire_archives(home, uid, device):
    """Delete this cleaner's own archives older than ARCHIVE_KEEP_SECONDS; returns how many.

    Run by --expire-archives, which the host driver sends on applying runs only.
    """
    cutoff = wall() - ARCHIVE_KEEP_SECONDS
    deleted = 0
    with as_owner():
        try:
            root = archive_dir_fd(home, uid, device)
        except (OSError, Incomplete):
            return 0
        try:
            for repo_dir in os.scandir(root):
                if not repo_dir.is_dir(follow_symlinks=False):
                    continue
                try:
                    sub = os.open(repo_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  dir_fd=root)
                except OSError:
                    continue
                try:
                    info = os.fstat(sub)
                    if info.st_uid != uid or info.st_dev != device:
                        continue  # not this cleaner's directory (another owner, a mount)
                    for entry in os.scandir(sub):
                        info = entry.stat(follow_symlinks=False)
                        if (ARCHIVE_NAME.fullmatch(entry.name) and stat.S_ISREG(info.st_mode)
                                and info.st_uid == uid and info.st_dev == device and info.st_mtime < cutoff):
                            os.unlink(entry.name, dir_fd=sub)
                            deleted += 1
                finally:
                    os.close(sub)
        finally:
            os.close(root)
    return deleted


def open_below(root_fd, parts):
    """Open a directory below root_fd one component at a time, never through a symlink."""
    fd = os.dup(root_fd)
    try:
        for part in parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


class Hashing:
    """A read-only stream that hashes what tarfile copies out of it."""

    def __init__(self, stream):
        self.stream, self.digest = stream, hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def ignored_entries(root_fd, roots, uid, device, end):
    """Yield (relative path, stat, parent fd, name) for every entry the archive keeps.

    A root that is a node_modules, .next or target directory (Git ignores the
    whole directory) is build output: it is not kept, but it is still searched,
    bounded, for a repository inside. Raises NotIdle for a repository anywhere,
    another filesystem, a file the account does not own, or a walk too large.
    """
    entries = 0

    def fail(error):
        raise NotIdle from error

    def check(info):
        nonlocal entries
        entries += 1
        if entries > WALK_LIMIT:
            raise NotIdle
        if clock() >= end:
            raise Incomplete("archive timed out")
        if info.st_dev != device or info.st_uid != uid:
            raise NotIdle  # another filesystem, or a file this account does not own

    for rel in roots:
        pfd = open_below(root_fd, rel.parts[:-1])
        try:
            info = os.stat(rel.name, dir_fd=pfd, follow_symlinks=False)
            check(info)
            build = stat.S_ISDIR(info.st_mode) and rel.name in BUILD_NAMES
            if not build:
                yield rel, info, pfd, rel.name
            if not stat.S_ISDIR(info.st_mode):
                continue
            for top, dirs, files, dfd in os.fwalk(rel.name, dir_fd=pfd, follow_symlinks=False, onerror=fail):
                if ".git" in dirs or ".git" in files:
                    raise NotIdle  # a repository inside is not ours to pack or delete
                here = rel.parent / top
                for name in dirs + files:
                    entry = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    check(entry)
                    if not build:
                        yield here / name, entry, dfd, name
        finally:
            os.close(pfd)


def fingerprint(root_fd, roots, uid, device, end):
    # ctime too: any write changes it, and unlike mtime it cannot be set back.
    return {os.fspath(rel): (stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                             info.st_ino)
            for rel, info, _, _ in ignored_entries(root_fd, roots, uid, device, end)}


def ignored_unchanged(path, ignored, before, uid, device, end):
    """The ignored files still match what was archived; checked again just before removal."""
    roots = [Path(r) for r in ignored]
    with as_owner():
        root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            return fingerprint(root_fd, roots, uid, device, end) == before
        finally:
            os.close(root_fd)


def archive_ignored(home, uid, device, repo, path, head, ignored, end):
    """Pack the worktree's ignored files into a verified archive; build output is left out.

    Returns (archive path or None when nothing would be kept, the fingerprint
    the archive matches, for ignored_unchanged just before removal). Raises
    NotIdle (keep the worktree) for anything it cannot copy faithfully: a nested
    repository (also inside build output), another filesystem, a file the
    account does not own, more than ARCHIVE_LIMIT bytes, too little free space,
    a file that changes while it is read or after it is archived, or an archive
    whose gzip stream or member contents do not read back exactly. Runs as the
    account. Symlinks are stored as links; sockets, FIFOs and devices are listed
    in the manifest as not archived.
    """
    roots = []
    for relative in ignored:
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise NotIdle
        roots.append(rel)
    if not roots:
        return None, {}
    with as_owner():
        root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            return _archive(home, uid, device, repo, path, head, roots, root_fd, end)
        finally:
            os.close(root_fd)


def _archive(home, uid, device, repo, path, head, roots, root_fd, end):
    before = fingerprint(root_fd, roots, uid, device, end)
    if not before:
        return None, before
    if sum(entry[1] for entry in before.values() if entry[0] == stat.S_IFREG) > ARCHIVE_LIMIT:
        raise NotIdle
    usage = os.statvfs(home)
    if usage.f_bavail * usage.f_frsize < ARCHIVE_SPACE:
        raise NotIdle  # too little room to write an archive safely
    folder = re.sub(r"[^A-Za-z0-9._-]", "_", repo.name)[:200]
    dfd = archive_dir_fd(home, uid, device, folder)
    try:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(wall()))
        where = hashlib.sha256(os.fsencode(path)).hexdigest()[:8]
        name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:200] + f"-{where}-{head[:12]}-{stamp}.tar.gz"
        part = "." + name + ".part"
        expected, skipped = {}, []
        copied = 0
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped, \
                        tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                    for rel, info, dir_fd, entry_name in ignored_entries(root_fd, roots, uid, device, end):
                        member = tarfile.TarInfo(os.fspath(Path("worktree") / rel))
                        member.mode, member.mtime = stat.S_IMODE(info.st_mode), int(info.st_mtime)
                        digest = None
                        if stat.S_ISDIR(info.st_mode):
                            member.type = tarfile.DIRTYPE
                            tar.addfile(member)
                        elif stat.S_ISLNK(info.st_mode):
                            member.type = tarfile.SYMTYPE
                            member.linkname = os.readlink(entry_name, dir_fd=dir_fd)
                            tar.addfile(member)
                        elif stat.S_ISREG(info.st_mode):
                            src = os.open(entry_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                          dir_fd=dir_fd)
                            with os.fdopen(src, "rb") as stream:
                                now = os.fstat(stream.fileno())
                                if (now.st_ino, now.st_size, now.st_mtime_ns) != (info.st_ino, info.st_size,
                                                                                  info.st_mtime_ns):
                                    raise NotIdle  # replaced or written since it was listed
                                member.size = now.st_size
                                copied += now.st_size
                                if copied > ARCHIVE_LIMIT:
                                    raise NotIdle  # grew past the limit since it was listed
                                reader = Hashing(stream)
                                tar.addfile(member, reader)
                                if os.fstat(stream.fileno()).st_mtime_ns != now.st_mtime_ns:
                                    raise NotIdle  # written to while being read
                                digest = reader.digest.hexdigest()
                        else:
                            skipped.append(os.fspath(rel))
                            continue
                        expected[member.name] = (member.type, member.size, member.linkname, digest)
                    manifest = json.dumps({"worktree": str(path), "repository": str(repo), "head": head,
                                           "archived_at": stamp, "members": len(expected),
                                           "not_archived": skipped}, indent=1).encode()
                    info = tarfile.TarInfo("MANIFEST.json")
                    info.size = len(manifest)
                    tar.addfile(info, io.BytesIO(manifest))
                raw.flush()
                os.fsync(raw.fileno())
            # Read it all back through gzip to its end (CRC and length trailer) and
            # compare every member's type, size, link target and content hash.
            check = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            seen = {}
            with os.fdopen(check, "rb") as raw, gzip.GzipFile(fileobj=raw, mode="rb") as unzipped:
                with tarfile.open(fileobj=unzipped, mode="r|") as tar:
                    for member in tar:
                        digest = None
                        if member.isfile():
                            h = hashlib.sha256()
                            stream = tar.extractfile(member)
                            while block := stream.read(1 << 20):
                                h.update(block)
                                if clock() >= end:
                                    raise Incomplete("archive check timed out")
                            digest = h.hexdigest()
                        seen[member.name] = (member.type, member.size, member.linkname, digest)
                while unzipped.read(1 << 20):
                    pass  # reaching EOF makes gzip verify its trailer
            if "MANIFEST.json" not in seen or any(seen.get(k) != v for k, v in expected.items()):
                raise NotIdle
            # Anything changed or added since it was listed would be lost: keep the tree.
            if fingerprint(root_fd, roots, uid, device, end) != before:
                raise NotIdle
            # Publish without replacing: an existing archive of that name is never overwritten.
            os.link(part, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            os.unlink(part, dir_fd=dfd)
            os.fsync(dfd)
        except (BaseException, gzip.BadGzipFile, EOFError, tarfile.TarError) as error:
            with contextlib.suppress(OSError):
                os.unlink(part, dir_fd=dfd)
            if isinstance(error, (gzip.BadGzipFile, EOFError, tarfile.TarError, FileExistsError)):
                raise NotIdle from error
            raise
        return home / ARCHIVE_DIR / folder / name, before
    finally:
        os.close(dfd)


def lone_zombie(proc, inode):
    # Linux has released a zombie's files and cwd. A zombie leader can still
    # have live threads, and a reused PID no longer describes the failed scan.
    try:
        lines = (proc / "status").read_bytes().splitlines()
        fields = dict(line.split(b":", 1) for line in lines if b":" in line)
        return (fields.get(b"State", b"").strip() == b"Z (zombie)"
                and fields.get(b"Threads", b"").strip() == b"1" and proc.stat().st_ino == inode)
    except OSError:
        return False


def protected_paths(home, uid, inspect_all=False, mapped=False):
    """Protect kernel paths and ndev registrations; production inspects every UID.

    With mapped (the build pass), each process's executable and the files it
    has mapped (/proc/<pid>/maps) count too, so a binary running from target/
    or a library loaded from node_modules keeps its worktree.
    """
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
                try:
                    with os.scandir(proc / "fd") as descriptors:
                        links = (proc / "cwd", *((proc / "exe",) if mapped else ()),
                                 *(Path(e.path) for e in descriptors))
                except PermissionError:
                    if lone_zombie(proc, entry.inode()):
                        continue
                    raise
                for link in links:
                    try:
                        target = Path(os.readlink(link).removesuffix(" (deleted)"))
                    except FileNotFoundError:
                        continue
                    if target.is_absolute() and under(target, home):
                        active.add(target)
                if mapped:
                    try:
                        with (proc / "maps").open("rb") as maps:
                            for line in maps:
                                fields = line.rstrip(b"\n").split(None, 5)
                                if len(fields) == 6 and fields[5].startswith(b"/"):
                                    target = Path(os.fsdecode(fields[5]).removesuffix(" (deleted)"))
                                    if under(target, home):
                                        active.add(target)
                    except FileNotFoundError:
                        pass  # a kernel thread, a fixture, or the process exited
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


def clean(home, path, head, directory, allow_ignored=False):
    """True when the checkout has nothing Git would lose.

    With allow_ignored, ignored files may remain and a list of their paths
    (relative, as Git reports them) is returned instead of True; untracked,
    modified, staged or hidden edits still return False.
    """
    actual = git(home, path, "rev-parse", "--verify", "HEAD")
    if actual is None or os.fsdecode(actual).strip() != head:
        return False
    if not (directory / "index").is_file() or any(directory.glob("*.lock")):
        return False
    if any((directory / name).exists() for name in OPERATIONS):
        return False
    marker = directory / "claude-interactive"
    if marker.exists() and time.time() - marker.stat().st_mtime < MARKER_AGE:
        return False
    if allow_ignored:
        status = git(home, path, "status", "--porcelain=v1", "-z", "--untracked-files=normal",
                     "--ignored=matching", "--ignore-submodules=none")
        if status is None:
            return False
        ignored = []
        for row in status.split(b"\0"):
            if not row:
                continue
            if not row.startswith(b"!! "):
                return False
            ignored.append(os.fsdecode(row[3:]).rstrip("/"))
        index = git(home, path, "ls-files", "-v", "-z")
        if index is None or not all(e.startswith(b"H ") for e in index.split(b"\0") if e):
            return False
        return ignored
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


def prune(home, uid, token=None, *, inspect_all=False, retire=False):
    skipped = {"status": "skipped"} if token else {"candidates": []}
    healthy = {"status": "healthy"} if token else {"candidates": []}
    if not pressure(home):
        return healthy
    end = clock() + RANK_SECONDS
    device = home.stat().st_dev
    if not owned(home, uid, device):
        raise OSError("unsafe home")
    directories = list(dict.fromkeys(roots(home, uid, device)))
    candidates = []
    registered = set(directories)
    for repo in directories:
        if clock() >= end:
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
    if clock() >= end:
        raise Incomplete("worktree discovery timed out")
    if not candidates or (token and len(candidates) != 1):
        return skipped
    paths = protected_paths(home, uid, inspect_all)
    retire_cutoff = int((wall() - RETIRE_IDLE_SECONDS) * 1e9)
    eligible = []
    origins = {}
    defaults = {}
    merge_proofs = set()
    valid_repos = {}
    for age, repo, path, record, info in sorted(candidates, key=lambda item: (item[0], item[2])):
        if clock() >= end:
            if token:
                raise Incomplete("worktree ranking timed out")
            # Candidates are ranked oldest first and each listed one passed
            # every check, so the list so far is a verified oldest-first prefix.
            # Removal rechecks each one; the next run ranks the rest.
            break
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
            is_clean = clean(home, path, head, info[0], allow_ignored=retire)
        except Incomplete:
            # A bounded Git read did not prove this checkout clean. Keep this
            # tree, but continue checking independently verified candidates.
            continue
        if is_clean is False:
            continue
        if repo not in origins:
            url = git(home, repo, "remote", "get-url", "origin")
            match = GITHUB.fullmatch(os.fsdecode(url).strip()) if url else None
            origins[repo] = match[1].lower() if match else None
        origin = origins[repo]
        if not origin:
            continue
        if origin not in defaults:
            defaults[origin] = remote_default(home, repo, origin)
        default = defaults[origin]
        if not default or record.get("branch") == default[0]:
            continue
        proof = (origin, head, *default)
        if proof not in merge_proofs:
            if merged(home, repo, head, *default, origin):
                merge_proofs.add(proof)
            elif not (retire and git_idle(home, path, info[0], uid, device, retire_cutoff) is not None
                      and on_remote(home, repo, head, origin)):
                # Retirement also takes an idle worktree whose HEAD is on a
                # live remote branch: nothing in Git would be lost.
                continue
        if not token:
            eligible.append({"token": info[2], "age_ns": age})
            if len(eligible) == LIST_LIMIT:
                break  # The oldest LIST_LIMIT; later runs reach the rest.
            continue
        # Recheck all non-Git protections, registration and worktree content just
        # before removal. Use Git's ordinary removal and keep every branch ref.
        records = worktrees(home, repo)
        if (record not in records or identity(repo, path, head, uid, device) != info
                or any(Path(x["worktree"]) != path and under(Path(x["worktree"]).resolve(), path)
                       for x in records if "worktree" in x)
                or protected(path, protected_paths(home, uid, inspect_all))):
            return skipped
        ignored = clean(home, path, head, info[0], allow_ignored=retire)
        if ignored is False:
            return skipped
        if not pressure(home):
            return healthy
        if retire and ignored:
            # git worktree remove deletes ignored files without asking. Keep them
            # in a read-back archive first; anything not archivable keeps the tree.
            try:
                _, archived = archive_ignored(home, uid, device, repo, path, head, ignored,
                                              clock() + ARCHIVE_SECONDS)
                # The archive took time: nothing may have changed meanwhile. The ignored
                # files are compared last, immediately before git removes them.
                if (identity(repo, path, head, uid, device) != info
                        or clean(home, path, head, info[0], allow_ignored=True) != ignored
                        or protected(path, protected_paths(home, uid, inspect_all))
                        or not ignored_unchanged(path, ignored, archived, uid, device, clock() + ARCHIVE_SECONDS)):
                    return skipped
            except (NotIdle, OSError):
                return skipped
        removed = git(home, repo, "worktree", "remove", "--", str(path), timeout=50) is not None
        return {"status": "removed" if removed else "declined"}
    if token and clock() >= end:
        raise Incomplete("worktree ranking timed out")
    return skipped if token else {"candidates": eligible}


class NotIdle(Exception):
    """A worktree whose idleness could not be proven; keep it."""


def git_idle(home, path, admin, uid, device, cutoff):
    """When the worktree was last used by Git, if that predates the cutoff.

    The newer of HEAD's commit time and the newest HEAD reflog entry (a
    commit, checkout, reset or rebase), in nanoseconds. Not file times of
    HEAD or index: any `git status` rewrites the index, so they record reads.
    None for anything else: a missing or unusual file, a lock, an operation in
    progress, a lock on the worktree or an interactive marker of any age.
    """
    try:
        if (any(os.path.lexists(admin / name) for name in ("claude-interactive", "locked", *OPERATIONS))
                or any(admin.glob("*.lock"))):
            return None
        for name in ("HEAD", "logs/HEAD"):
            info = (admin / name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_dev != device:
                return None
        fd = os.open(admin / "logs/HEAD", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as log:
            log.seek(max(0, os.fstat(log.fileno()).st_size - 4096))
            last = log.read(4096).rstrip(b"\n").rpartition(b"\n")[2]
    except OSError:
        return None
    reflog = REFLOG_TIME.search(last)
    commit = git(home, path, "show", "-s", "--format=%ct", "HEAD")
    if not reflog or commit is None or not commit.strip().isdigit():
        return None
    newest = max(int(reflog[1]), int(commit)) * 10**9
    return newest if newest < cutoff else None


def build_ignored(home, path, relative):
    """Git ignores this directory in the worktree and tracks nothing inside it."""
    # check-ignore takes paths, not pathspecs (it refuses literal magic); a
    # pattern-like parent name at worst reads as not ignored, which keeps it.
    # check-ignore skips tracked paths, not tracked files below an ignored
    # directory, so ls-files (literal) must also find nothing there.
    name = os.fspath(relative)
    return (git(home, path, "check-ignore", "-q", "--", name) == b""
            and git(home, path, "--literal-pathspecs", "ls-files", "-z", "--", name) == b"")


def survey(home, path, uid, device, cutoff, end, verified=(), ask=None):
    """List a worktree's removable build directories, proving every other file idle.

    Returns [(relative path, (st_dev, st_ino))]. Raises NotIdle when a file
    outside those directories was modified since the cutoff, a directory below
    the root holds a .git entry (a nested clone answers ignore questions for
    itself, not the outer worktree), the tree crosses a filesystem or cannot be
    read, and Incomplete when the time budget runs out. Reads metadata only
    and follows no symlink. A recheck passes the first survey's result as
    verified: those directories, at the same inode, are not asked about again,
    except ask, the one about to be removed.
    """
    found = []
    entries = 0

    def fail(error):
        raise NotIdle from error

    for top, directories, files, fd in os.fwalk(os.fspath(path), follow_symlinks=False, onerror=fail):
        depth = len(Path(top).relative_to(path).parts)
        descend = []
        for name, listed_dir in itertools.chain(((d, True) for d in directories), ((f, False) for f in files)):
            entries += 1
            if entries > WALK_LIMIT:
                raise NotIdle
            if clock() >= end:
                raise Incomplete("build survey timed out")
            if name == ".git" and depth:
                raise NotIdle
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as error:
                raise NotIdle from error
            if info.st_dev != device:
                raise NotIdle
            if stat.S_ISDIR(info.st_mode):
                if not listed_dir:
                    raise NotIdle  # replaced while listing
                relative = Path(top, name).relative_to(path)
                key = (relative, (info.st_dev, info.st_ino))
                if (name in BUILD_NAMES and depth < BUILD_DEPTH and info.st_uid == uid
                        and (key in verified and relative != ask or build_ignored(home, path, relative))):
                    found.append(key)
                else:
                    descend.append(name)
            # Any other entry, a symlink included, is a file that must be idle.
            # Not ctime: removing or adding any hard link to a file changes it.
            elif info.st_mtime_ns >= cutoff:
                raise NotIdle
        directories[:] = descend
    return found


def reraise(error):
    raise error


def measure(directory, device, end):
    """Bytes removing a directory frees: entries with no other hard link.

    None if unsure, including a .git entry anywhere inside (a repository).
    """
    try:
        info = directory.lstat()
        total = info.st_blocks * 512
        for _, directories, files, fd in os.fwalk(os.fspath(directory), follow_symlinks=False, onerror=reraise):
            for name in itertools.chain(directories, files):
                if clock() >= end:
                    raise Incomplete("build measurement timed out")
                if name == ".git":
                    return None
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if entry.st_dev != device:
                    return None
                # A pnpm store file is hard-linked into node_modules; removing the link frees nothing.
                if stat.S_ISDIR(entry.st_mode) or entry.st_nlink == 1:
                    total += entry.st_blocks * 512
    except OSError:
        return None
    return total


def inode(path):
    info = path.lstat()
    return info.st_dev, info.st_ino


def builds(home, uid, apply, after=None, *, inspect_all=False):
    """One time-boxed pass over idle linked worktrees, oldest first.

    Ranks worktrees by Git use (git_idle), then reports (or, with apply,
    removes) their ignored node_modules, .next and target directories.
    Returns counts and a cursor only, never a path.
    """
    result = {"status": "healthy", "worktrees": 0, "dirs": 0, "bytes": 0, "failed": 0, "next": None}
    if not pressure(home):
        return result
    end = clock() + BUILD_SECONDS
    device = home.stat().st_dev
    if not owned(home, uid, device):
        raise OSError("unsafe home")
    cutoff = int((wall() - IDLE_SECONDS) * 1e9)
    directories = list(dict.fromkeys(roots(home, uid, device)))
    registered = set(directories)
    candidates = []
    for repo in directories:
        if clock() >= end:
            raise Incomplete("repository discovery timed out")
        # The first record, the main checkout, is never a candidate.
        for record in worktrees(home, repo):
            path = Path(record["worktree"])
            if path == home or not under(path, home):
                continue
            registered.add(path.resolve())
            if len(registered) > 4096:
                raise Incomplete("worktree discovery exceeded limit")
            if {"bare", "locked", "prunable"} & record.keys():
                continue
            # identity() proves a linked worktree: a .git file naming this repository's admin directory.
            info = identity(repo, path, record.get("HEAD", ""), uid, device)
            age = git_idle(home, path, info[0], uid, device, cutoff) if info else None
            if age is not None:
                key = (age, hashlib.sha256(os.fsencode(path)).hexdigest())
                if after is None or key > after:
                    candidates.append((key, path, info[0]))
    candidates.sort()
    paths = protected_paths(home, uid, inspect_all, mapped=True)
    result["status"] = "done"
    for key, path, admin in candidates:
        # Always take one candidate, so every pass advances the cursor.
        if result["next"] is not None and clock() >= end:
            result["status"] = "more"
            break
        if apply and not pressure(home):
            result["status"] = "healthy"
            break
        result["next"] = "%d:%s" % key
        if protected(path, paths) or any(p != path and under(p, path) for p in registered):
            continue
        try:
            found = survey(home, path, uid, device, cutoff, end)
        except (NotIdle, Incomplete):
            continue
        counted = freed = 0
        stop = False
        for relative, expected in found:
            directory = path / relative
            if clock() >= end + DELETE_SECONDS:
                stop = True
                break
            try:
                size = measure(directory, device, end + DELETE_SECONDS)
                if size is None:
                    continue
                # The recheck is the last step before removal: survey again (the
                # same real, ignored, untracked directory by inode; every other
                # file still idle; no nested clone), the worktree's Git use
                # unchanged, and no process, mapping, mount or published site in it.
                try:
                    current = survey(home, path, uid, device, cutoff, end + DELETE_SECONDS, found, relative)
                    same = owned(directory, uid, device) and inode(directory) == expected
                except (NotIdle, OSError):
                    break
                if not ((relative, expected) in current and same
                        and git_idle(home, path, admin, uid, device, cutoff) == key[0]
                        and not protected(path, protected_paths(home, uid, inspect_all, mapped=True))):
                    break
                if apply and run(home, ["/usr/bin/rm", "-rf", "--one-file-system", "--", os.fspath(directory)],
                                 timeout=DELETE_SECONDS) is None:
                    # rm removed what the account may remove; the rest (a
                    # root-owned subdirectory, say) stays. Count it once, go on.
                    result["failed"] += 1
                    continue
            except Incomplete:
                # Out of time mid-directory; a partly removed build directory is still regenerable.
                result["failed"] += int(apply)
                stop = True
                break
            counted += 1
            freed += size
        if counted:
            result["worktrees"] += 1
            result["dirs"] += counted
            result["bytes"] += freed
        if stop:
            result["status"] = "more"
            break
    return result


def main():
    global COMMAND_OWNER
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", metavar="ACCOUNT", help="inspect every process, run Git as this account")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list eligible candidates without removing anything")
    mode.add_argument("--remove", metavar="TOKEN", help="revalidate and remove one listed candidate")
    mode.add_argument("--builds", action="store_true",
                      help="count idle worktrees' ignored build output without removing anything")
    mode.add_argument("--clean-builds", action="store_true",
                      help="remove idle worktrees' ignored build output while the disk is pressured")
    parser.add_argument("--after", metavar="CURSOR", help="continue a build pass after this cursor")
    mode.add_argument("--expire-archives", action="store_true",
                      help="delete this cleaner's archives older than 90 days (the driver's applying runs only)")
    parser.add_argument("--retire", action="store_true",
                        help="with --list or --remove: archive ignored files, then remove; also take idle"
                             " worktrees whose HEAD is on a live remote branch")
    args = parser.parse_args()
    if args.retire and (args.builds or args.clean_builds or args.expire_archives):
        parser.error("--retire applies to --list and --remove")
    if args.remove and not TOKEN.fullmatch(args.remove):
        parser.error("invalid worktree token")
    after = None
    if args.after is not None:
        match = CURSOR.fullmatch(args.after)
        if not match or not (args.builds or args.clean_builds):
            parser.error("--after takes a cursor from a build pass")
        after = (int(match[1]), match[2])
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
        if args.expire_archives:
            device = home.stat().st_dev
            if not owned(home, uid, device):
                raise OSError("unsafe home")
            result = {"deleted": expire_archives(home, uid, device)}
        elif args.builds or args.clean_builds:
            result = builds(home, uid, args.clean_builds, after, inspect_all=root)
        else:
            result = prune(home, uid, args.remove, inspect_all=root, retire=args.retire)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, Incomplete) as error:
        if not args.remove:
            # The host discards container stderr. Keep diagnostics in this account's journal;
            # OS errors may contain paths, so record only errno and our own source locations.
            reason = (str(error) if type(error) is Incomplete else "") or type(error).__name__
            if isinstance(error, OSError) and error.errno is not None:
                reason += f" errno={error.errno}"
            stages = " > ".join(f"{frame.f_code.co_name}:{line}" for frame, line in
                                traceback.walk_tb(error.__traceback__) if frame.f_globals is globals())
            syslog.openlog("nextjs-worktree-prune-worker")
            syslog.syslog(syslog.LOG_WARNING, f"Account listing failed: {reason} ({stages})")
            print("worktree-prune: unable to rank account completely", file=sys.stderr)
            return 1
        result = {"status": "skipped"}
    finally:
        COMMAND_OWNER = None
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

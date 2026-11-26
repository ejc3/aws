#!/usr/bin/python3 -I
"""Free space on a parallel box's persistent work volume (/mnt/work), oldest and most regenerable first.

Each slot's /mnt/work survives every launch, and agents leave checkouts, Cargo target directories, node_modules and
logs there. This runs hourly and once after boot (pbox-work-clean.tf) and removes, in order:

  1. build caches (node_modules, .next, .open-next, __pycache__, .pnpm-store, a .venv with pyvenv.cfg, an .npm with
     _cacache, a Cargo target, any directory with a CACHEDIR.TAG) whose newest entry is older than CACHE_IDLE;
  2. whole top-level task directories (/mnt/work/*) whose newest entry is older than TASK_IDLE, or CACHE_IDLE for a
     top-level directory that is itself a cache (a shared CARGO_TARGET_DIR, a pnpm store);
  3. while the volume is over HIGH full (bytes or inodes), the oldest remaining task directories until it is under LOW.

A top-level directory is never touched, caches included, when:
  - a process has its cwd, root, executable, an open file or a mapping inside it, a loop device is backed by a file in
    it, or it holds the source of a bind mount (checked by inode, so a process in a container counts too);
  - anything inside it was modified in the last RECENT (directories by mtime, everything else by mtime or ctime, so a
    tree copied with its old times still counts as new);
  - it contains a .pbox-keep anywhere inside it (/mnt/work/.pbox-keep keeps the whole volume);
  - another filesystem is mounted inside it, or any part of it cannot be read.
Both checks run twice, once to choose and again just before removal. Nothing outside /mnt/work is read for removal or
written: the walk never follows a symlink or leaves the work filesystem, removal is a rename into
/mnt/work/.pbox-clean-trash and then `rm --one-file-system`, and the unit can write only /mnt/work and its lock.

Each removal and a summary go to the journal; the summary is also the one line of /mnt/work/.pbox-clean.log.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path("/mnt/work")
PROC = Path("/proc")
KEEP = ".pbox-keep"
TRASH = ".pbox-clean-trash"
LOG = ".pbox-clean.log"
# Top-level names that are never a task directory.
RESERVED_TOP = frozenset({"lost+found", TRASH})

HOUR_NS = 3600 * 10**9
RECENT = 24 * HOUR_NS
CACHE_IDLE = 48 * HOUR_NS
TASK_IDLE = 7 * 24 * HOUR_NS
HIGH = 0.85
LOW = 0.70
# The unit's TimeoutStartSec is 45 minutes; stop choosing and removing after this, and let rm run 5 more.
DEADLINE_SECONDS = 35 * 60
RM_SECONDS = 15 * 60

CACHE_NAMES = frozenset({"node_modules", ".next", ".open-next", "__pycache__", ".pnpm-store"})
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
# Filesystems a stat may block on (the I/O box's NFS scratch, FUSE): links into them are not followed.
REMOTE_TYPES = ("nfs", "fuse", "autofs", "cifs", "smb", "ceph", "9p", "glusterfs", "sshfs")


class Unsafe(Exception):
    """This directory cannot be proven safe to remove (a mount inside, an unreadable entry, a race)."""


class Recent(Exception):
    """Something inside was modified within the cutoff; stop walking."""


class OutOfTime(Exception):
    """The run's deadline passed."""


def shown(path):
    """A path for the journal: quoted, control characters escaped."""
    return json.dumps(os.fsdecode(path))


def unescape(field):
    """A mountinfo field: spaces, tabs, newlines and backslashes are octal escapes."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and len(field[i + 1:i + 4]) == 3 and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mounts(text):
    """[(major:minor, root, mount point, fstype)] from a mountinfo."""
    rows = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        rows.append((fields[2], unescape(fields[3]), unescape(fields[4]), right.split()[0]))
    return rows


def devname(device):
    return f"{os.major(device)}:{os.minor(device)}"


def under(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def in_use(device, root=ROOT):
    """Inodes on the work filesystem that something is using: every process's cwd, root, executable, open files and
    mappings; loop devices' backing files; and the source directories of bind mounts, in every mount namespace."""
    inodes = set()
    work = devname(device)
    namespaces = {}
    strict = os.geteuid() == 0
    for entry in os.scandir(PROC):
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        proc = entry.path
        try:
            namespace = os.readlink(f"{proc}/ns/mnt")
            if namespace not in namespaces:
                with open(f"{proc}/mountinfo") as f:
                    rows = mounts(f.read())
                for dev, source, _, _ in rows:
                    # A bind mount of a directory on the work volume: its source is in use.
                    if dev == work and source != "/":
                        try:
                            info = os.lstat(os.path.join(root, source.lstrip("/")))
                        except OSError:
                            continue
                        if info.st_dev == device:
                            inodes.add(info.st_ino)
                namespaces[namespace] = [point for _, _, point, kind in rows if kind.startswith(REMOTE_TYPES)]
            remote = namespaces[namespace]
            links = [f"{proc}/cwd", f"{proc}/root", f"{proc}/exe"]
            try:
                links.extend(fd.path for fd in os.scandir(f"{proc}/fd"))
            except FileNotFoundError:
                pass
            for link in links:
                try:
                    target = os.readlink(link).removesuffix(" (deleted)")
                    if not target.startswith("/") or any(under(target, point) for point in remote):
                        continue  # a socket or pipe, or a file on a filesystem whose stat may block
                    info = os.stat(link)  # follows the kernel's link to the open file itself
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if info.st_dev == device:
                    inodes.add(info.st_ino)
            with open(f"{proc}/maps", "rb") as f:
                for line in f:
                    fields = line.split(None, 5)
                    if len(fields) >= 5 and fields[4] != b"0":
                        major, _, minor = fields[3].partition(b":")
                        if f"{int(major, 16)}:{int(minor, 16)}" == work:
                            inodes.add(int(fields[4]))
        except (FileNotFoundError, ProcessLookupError):
            continue  # the process exited
        except PermissionError:
            if strict:
                raise
            continue  # only when not root, in the offline tests: other users' processes
    listing = subprocess.run(["/usr/sbin/losetup", "--list", "--json", "--output", "BACK-INO,BACK-MAJ:MIN"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True)
    for loop in json.loads(listing.stdout or b'{"loopdevices": []}').get("loopdevices", []):
        if str(loop.get("back-maj:min")) == work and str(loop.get("back-ino") or "").isdecimal():
            inodes.add(int(loop["back-ino"]))
    return inodes


class Tree:
    """What one walk found: the newest change, the bytes removal frees, and the reasons to keep it."""

    def __init__(self, info):
        self.ino = info.st_ino
        self.newest = info.st_mtime_ns
        self.bytes = info.st_blocks * 512
        self.keep = False
        self.busy = False
        self.caches = []
        self.cache = False

    def absorb(self, other):
        self.newest = max(self.newest, other.newest)
        self.bytes += other.bytes
        self.keep |= other.keep
        self.busy |= other.busy


def is_cache(name, fd, dirs, files):
    """Regenerable build output, by name or by the marker its tool writes."""
    if name in CACHE_NAMES:
        return True
    if name == ".venv" and "pyvenv.cfg" in files:
        return True
    if name == ".npm" and "_cacache" in dirs:
        return True
    if name == "target" and ".rustc_info.json" in files:
        return True
    if "CACHEDIR.TAG" in files:
        try:
            tag = os.open("CACHEDIR.TAG", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except OSError:
            return False
        with os.fdopen(tag, "rb") as f:
            return stat.S_ISREG(os.fstat(f.fileno()).st_mode) and f.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE
    return False


def walk(path, device, inuse, end, clock, cutoff=None, find_caches=False):
    """Walk one directory tree without following a symlink or leaving the work filesystem.

    With cutoff, raise Recent as soon as an entry is newer. With find_caches, build caches below the top are not
    walked here: they are listed in .caches for the caller to walk on their own.
    """
    try:
        info = os.lstat(path)
    except OSError as error:
        raise Unsafe("unreadable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
        raise Unsafe("not a directory on the work volume")
    tree = Tree(info)
    tree.busy = info.st_ino in inuse
    if cutoff is not None and tree.newest >= cutoff:
        raise Recent

    def fail(error):
        raise Unsafe("unreadable") from error

    for top, dirs, files, fd in os.fwalk(path, follow_symlinks=False, onerror=fail):
        if find_caches and is_cache(os.path.basename(top), fd, dirs, files):
            if top == os.fspath(path):
                tree.cache = True
            else:
                tree.caches.append(top)
                dirs[:] = []
                continue
        descend = []
        for name, listed_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
            if clock() >= end:
                raise OutOfTime
            if name == KEEP:
                tree.keep = True
            try:
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as error:
                raise Unsafe("unreadable") from error
            if entry.st_dev != device:
                raise Unsafe("another filesystem is mounted inside")
            if entry.st_ino in inuse:
                tree.busy = True
            if stat.S_ISDIR(entry.st_mode):
                if not listed_dir:
                    raise Unsafe("changed while listing")
                descend.append(name)
                newest = entry.st_mtime_ns
            else:
                # A symlink listed as a directory is a symlink: never descended, timed like a file.
                newest = max(entry.st_mtime_ns, entry.st_ctime_ns)
            tree.newest = max(tree.newest, newest)
            if cutoff is not None and newest >= cutoff:
                raise Recent
            if stat.S_ISDIR(entry.st_mode) or entry.st_nlink == 1:
                tree.bytes += entry.st_blocks * 512  # a hard link elsewhere (a pnpm store) frees nothing
        dirs[:] = descend
    return tree


def survey(root, name, device, inuse, now, end, clock):
    """One task directory with its caches, or None when it is recent. Raises Unsafe."""
    path = os.path.join(root, name)
    task = walk(path, device, inuse, end, clock, cutoff=now - RECENT, find_caches=True)
    caches = []
    for cache_path in task.caches:
        cache = walk(cache_path, device, inuse, end, clock)
        cache.path = cache_path
        task.absorb(cache)
        if task.newest >= now - RECENT:
            return None
        caches.append(cache)
    task.caches = caches
    return None if task.newest >= now - RECENT else task


def usage_of(root):
    """The fuller of bytes and inodes, as df counts them (used over used plus available)."""
    fs = os.statvfs(root)
    used = fs.f_blocks - fs.f_bfree
    bytes_used = used / (used + fs.f_bavail) if used + fs.f_bavail else 0.0
    inodes_used = fs.f_files - fs.f_ffree
    inode_use = inodes_used / (inodes_used + fs.f_favail) if inodes_used + fs.f_favail else 0.0
    return max(bytes_used, inode_use), (used + fs.f_bavail) * fs.f_frsize


def open_dir(parent_fd, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)


def trash_fd(root_fd):
    """/mnt/work/.pbox-clean-trash: a real directory, root's own when the cleaner runs as root."""
    try:
        os.mkdir(TRASH, 0o700, dir_fd=root_fd)
    except FileExistsError:
        pass
    fd = open_dir(root_fd, TRASH)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_dev != os.fstat(root_fd).st_dev:
        os.close(fd)
        raise Unsafe("the trash directory is not the cleaner's own")
    return fd


def empty_trash(root, root_fd, clock, end, names=None):
    """Delete what was moved aside: the named entries, or everything (an earlier run's leftovers too). Returns how
    many entries remain."""
    if names is None:
        fd = trash_fd(root_fd)
        try:
            names = os.listdir(fd)
        finally:
            os.close(fd)
    left = 0
    for name in names:
        # Inside the unit's 45 minutes: what rm has not finished stays in the trash for the next run.
        remaining = max(30, min(RM_SECONDS, end + 5 * 60 - clock()))
        try:
            subprocess.run(["/usr/bin/rm", "-rf", "--one-file-system", "--", os.path.join(root, TRASH, name)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=remaining, check=True)
        except (subprocess.SubprocessError, OSError):
            left += 1  # what is left goes on the next run
    return left


def move_aside(root, root_fd, rel, expected_ino):
    """Rename root/rel into the trash, after checking it is the directory that was surveyed, and return its name
    there. Keeps the parent's mtime, so removing a cache does not make its task directory look recently used."""
    parts = Path(rel).parts
    if not parts or any(p in ("", ".", "..") for p in parts) or os.path.isabs(rel):
        raise Unsafe("bad path")
    fds = [root_fd]
    try:
        for part in parts[:-1]:
            fds.append(open_dir(fds[-1], part))
        parent = fds[-1]
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_ino != expected_ino or info.st_dev != os.fstat(root_fd).st_dev:
            raise Unsafe("changed since it was surveyed")
        before = os.fstat(parent)
        trash = trash_fd(root_fd)
        name = f"{time.time_ns()}-{uuid.uuid4().hex}"
        try:
            os.rename(parts[-1], name, src_dir_fd=parent, dst_dir_fd=trash)
        finally:
            os.close(trash)
        if parent != root_fd:
            os.utime(parent, ns=(before.st_atime_ns, before.st_mtime_ns))
        return name
    finally:
        for fd in fds[1:]:
            os.close(fd)


def idle_days(now, newest):
    return f"{(now - newest) / (24 * HOUR_NS):.1f}"


def clean(root, device, now, apply, *, usage=usage_of, scan=in_use, clock=time.monotonic, log=print):
    """One run. Returns the summary line."""
    root = os.fspath(root)
    end = clock() + DEADLINE_SECONDS
    counts = dict(caches=0, tasks=0, pressure=0, kept=0, busy=0, recent=0, unsafe=0)
    freed = 0
    start_use, total = usage(root)

    def current_use():
        # A dry run removes nothing, so estimate from what it would have freed.
        if apply:
            return usage(root)[0]
        return max(0.0, start_use - freed / total) if total else start_use
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if os.path.lexists(os.path.join(root, KEEP)):
            summary = f"{root}/{KEEP} keeps the whole volume; nothing removed (use {start_use:.0%})"
            log(f"pbox-work-clean: {summary}")
            return summary
        if apply:
            empty_trash(root, root_fd, clock, end)
        inuse = scan(device, root)
        tasks = {}
        names = sorted(e.name for e in os.scandir(root)
                       if e.name not in RESERVED_TOP and e.is_dir(follow_symlinks=False))
        for name in names:
            try:
                task = survey(root, name, device, inuse, now, end, clock)
            except Recent:
                task = None
            except Unsafe as error:
                log(f"pbox-work-clean: keeping {shown(os.path.join(root, name))}: {error}")
                counts["unsafe"] += 1
                continue
            except OutOfTime:
                log("pbox-work-clean: out of time while choosing; the rest waits for the next run")
                break
            if task is None:
                counts["recent"] += 1
            elif task.keep:
                counts["kept"] += 1
            elif task.busy:
                counts["busy"] += 1
            else:
                tasks[name] = task

        def recheck(name, task, idle):
            """The same directory, still unused, unkept and idle, by a fresh scan and walk. None when it is not."""
            try:
                again = survey(root, name, device, scan(device, root), now, end, clock)
            except (Recent, Unsafe):
                return None
            if again is None or again.ino != task.ino or again.keep or again.busy or again.newest >= now - idle:
                return None
            return again

        moved = []

        def remove(rel, ino, size, what, newest):
            """Move one directory aside; flush() then deletes it."""
            nonlocal freed
            verb = "removed" if apply else "would remove"
            if apply:
                moved.append(move_aside(root, root_fd, rel, ino))
            freed += size
            log(f"pbox-work-clean: {verb} {what} {shown(os.path.join(root, rel))}: {size} bytes, idle "
                f"{idle_days(now, newest)} days")

        def remove_task(name, task, idle, what):
            again = recheck(name, task, idle) if apply else task
            if again is None:
                log(f"pbox-work-clean: keeping {shown(os.path.join(root, name))}: in use or changed since it was chosen")
                return False
            try:
                remove(name, again.ino, again.bytes, what, again.newest)
            except (Unsafe, OSError) as error:
                log(f"pbox-work-clean: keeping {shown(os.path.join(root, name))}: {type(error).__name__}")
                return False
            del tasks[name]
            flush()
            return True

        def flush():
            """Delete what was just moved aside, so the space is free before the volume is measured again."""
            if moved:
                left = empty_trash(root, root_fd, clock, end, list(moved))
                moved.clear()
                if left:
                    log(f"pbox-work-clean: {left} trash entries not yet deleted; the next run retries")

        try:
            # 1 and 2: idle caches, idle task directories.
            for name, task in list(tasks.items()):
                idle = CACHE_IDLE if task.cache else TASK_IDLE
                if task.newest < now - idle:
                    if remove_task(name, task, idle, "cache" if task.cache else "task directory"):
                        counts["tasks"] += 1
                    continue
                stale = [c for c in task.caches if c.newest < now - CACHE_IDLE]
                if not stale:
                    continue
                again = recheck(name, task, RECENT) if apply else task
                if again is None:
                    log(f"pbox-work-clean: keeping {shown(os.path.join(root, name))}: in use or changed since it was "
                        "chosen")
                    continue
                current = {c.path: c for c in again.caches}
                for cache in stale:
                    now_cache = current.get(cache.path)
                    if now_cache is None or now_cache.ino != cache.ino or now_cache.newest >= now - CACHE_IDLE:
                        continue
                    try:
                        remove(os.path.relpath(cache.path, root), cache.ino, now_cache.bytes, "cache", now_cache.newest)
                    except (Unsafe, OSError) as error:
                        log(f"pbox-work-clean: keeping {shown(cache.path)}: {type(error).__name__}")
                        continue
                    counts["caches"] += 1
                    task.bytes -= now_cache.bytes
                # Every stale cache of this task was moved aside right after its recheck; now delete them.
                flush()

            # 3: under pressure, the oldest task directories that are still left, down to LOW.
            use = current_use()
            if use > HIGH:
                for name, task in sorted(tasks.items(), key=lambda kv: (kv[1].newest, kv[0])):
                    if use < LOW:
                        break
                    if clock() >= end:
                        raise OutOfTime
                    if remove_task(name, task, RECENT, "task directory under pressure"):
                        counts["pressure"] += 1
                        use = current_use()
        except OutOfTime:
            log("pbox-work-clean: out of time while removing; the rest waits for the next run")
        if apply:
            left = empty_trash(root, root_fd, clock, end)
            if left:
                log(f"pbox-work-clean: {left} trash entries not yet deleted; the next run retries")
            else:
                os.rmdir(TRASH, dir_fd=root_fd)
        end_use = current_use()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now / 10**9))
        summary = (f"{stamp} {'freed' if apply else 'would free'} {freed} bytes ({freed / 1024**3:.1f} GiB): "
                   f"{counts['caches']} caches, {counts['tasks']} idle task directories, {counts['pressure']} under "
                   f"pressure; use {start_use:.0%} -> {end_use:.0%}; kept {counts['kept']} (.pbox-keep), "
                   f"{counts['busy']} in use, {counts['recent']} recent, {counts['unsafe']} unreadable or mounted")
        log(f"pbox-work-clean: {summary}")
        if apply:
            write_log(root_fd, summary)
        return summary
    finally:
        os.close(root_fd)


def write_log(root_fd, line):
    """/mnt/work/.pbox-clean.log: the last run's one-line summary, replaced whole."""
    part = f"{LOG}.{uuid.uuid4().hex}"
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=root_fd)
    with os.fdopen(fd, "w") as f:
        f.write(line + "\n")
    os.rename(part, LOG, src_dir_fd=root_fd, dst_dir_fd=root_fd)


def work_volume(root=ROOT):
    """The work volume's device, or None when it is not mounted. Raises Unsafe when it is not a separate ext4."""
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise Unsafe(f"{root} is not a directory")
    if not os.path.ismount(root):
        return None
    if info.st_dev == os.stat("/").st_dev:
        raise Unsafe(f"{root} is on the root filesystem")
    kinds = [kind for _, _, point, kind in mounts((PROC / "self/mountinfo").read_text()) if point == os.fspath(root)]
    if not kinds or kinds[-1] != "ext4":
        raise Unsafe(f"{root} is not the ext4 work volume")
    return info.st_dev


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="remove; without it, only print what would be removed")
    args = parser.parse_args()
    if os.geteuid():
        parser.exit(2, "pbox-work-clean: run as root\n")
    try:
        device = work_volume()
        if device is None:
            print(f"pbox-work-clean: {ROOT} is not mounted; skipped")
            return
        clean(ROOT, device, time.time_ns(), args.apply)
    except (OSError, Unsafe, subprocess.SubprocessError, ValueError) as error:
        parser.exit(1, f"pbox-work-clean: stopped ({type(error).__name__}: {error})\n")


if __name__ == "__main__":
    main()

#!/usr/bin/python3 -I
"""Plan a bounded cleanup in global age order without running Git as root."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

CONFIG = Path("/etc/nextjs-ct/config.json")
WORKER = Path("/usr/local/lib/nextjs-worktree-prune/worker.py")
PROC = Path("/proc")
ACCOUNT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
TOKEN = re.compile(r"[0-9a-f]{64}\Z")
MAX_OUTPUT = 64 * 1024
MAX_CANDIDATES = 512
COMMAND_SECONDS = 300
USER_PATH = "/usr/local/bin:/usr/bin:/bin"
MAX_HOST_PIDS = 8192
MAX_HOST_LINKS = 65536


@dataclass(frozen=True, order=True)
class Candidate:
    age_ns: int
    account: str
    token: str


def pressured():
    # The host and the ID-mapped account homes share this disk.
    fs = os.statvfs("/home")
    return (fs.f_blocks > 0 and fs.f_bavail * 10 <= fs.f_blocks
            or fs.f_bavail * fs.f_frsize < 20 * 1024**3
            or fs.f_files > 0 and fs.f_favail * 10 <= fs.f_files)


def managed_accounts():
    with CONFIG.open("rb") as f:
        data = f.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("managed account configuration is too large")
    accounts = json.loads(data)["users"]
    if (not isinstance(accounts, dict) or not accounts or len(accounts) > 64
            or "ubuntu" in accounts or not all(ACCOUNT.fullmatch(a) for a in accounts)):
        raise ValueError("invalid managed account list")
    # Get an authoritative machine list first: a machine manager failure is
    # not evidence that configured accounts are offline.
    listing = subprocess.run(
        ["/usr/bin/machinectl", "list", "--no-legend", "--no-pager"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
    )
    present = {row.split()[0].decode() for row in listing.stdout.splitlines() if row.split()}
    running = ["ubuntu"]
    for account in sorted(accounts):
        if account not in present:
            print(f"nextjs-worktree-prune: {account} is offline; skipped")
            continue
        state = subprocess.run(
            ["/usr/bin/machinectl", "show", account, "-p", "State", "--value"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
        )
        if state.stdout.strip() == b"running":
            running.append(account)
        else:
            print(f"nextjs-worktree-prune: {account} is not running; skipped")
    return running


def worker_command(account, args):
    if not ACCOUNT.fullmatch(account):
        raise ValueError("invalid worker account")
    python = ["/usr/bin/python3", "-I", "-", "--owner", account, *args]
    if account == "ubuntu":
        return python
    # Run the inspector as root inside this machine so it can see other UIDs'
    # live process metadata. The worker drops UID/GID before any Git command.
    # Host root does not traverse the container's mutable filesystem.
    return ["/usr/bin/systemd-run", f"--machine={account}",
            "--wait", "--pipe", "--collect", "--service-type=exec", "--quiet", "--no-ask-password",
            "--nice=15", "--property=IOSchedulingClass=idle", "--property=NoNewPrivileges=yes",
            f"--property=RuntimeMaxSec={COMMAND_SECONDS}s", f"--working-directory=/home/{account}",
            f"--setenv=HOME=/home/{account}", f"--setenv=PATH={USER_PATH}", *python]


def query_worker(account, *args):
    """Bound stdout from the account; do not treat it as trusted host commands."""
    end = time.monotonic() + COMMAND_SECONDS + 10
    data = bytearray()
    with WORKER.open("rb") as script, subprocess.Popen(
        worker_command(account, args), stdin=script, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd="/",
    ) as child:
        assert child.stdout is not None
        try:
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                    raise TimeoutError("account worker timed out")
                block = os.read(child.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > MAX_OUTPUT:
                    raise ValueError("account worker output exceeded its bound")
            if child.wait(timeout=max(0, end - time.monotonic())):
                raise ValueError("account worker failed")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
            raise
    return json.loads(data)


def candidates_from(account, answer):
    if not isinstance(answer, dict) or answer.keys() != {"candidates"}:
        raise ValueError(f"invalid worktree list from {account}")
    rows = answer["candidates"]
    if not isinstance(rows, list) or len(rows) > MAX_CANDIDATES:
        raise ValueError(f"invalid worktree list size from {account}")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.keys() != {"token", "age_ns"}:
            raise ValueError(f"invalid worktree record from {account}")
        token, age = row["token"], row["age_ns"]
        if (not isinstance(token, str) or not TOKEN.fullmatch(token) or token in seen
                or type(age) is not int or not 0 <= age < 2**63):
            raise ValueError(f"invalid worktree identity or age from {account}")
        seen.add(token)
        result.append(Candidate(age, account, token))
    return result


def host_busy_accounts(accounts):
    """Preserve container homes in use by a process outside the container."""
    targets = set(accounts) - {"ubuntu"}
    if not targets:
        return set()
    namespace = os.readlink(PROC / "self/ns/pid")
    busy = set()
    pids = links = 0

    def check(link):
        nonlocal links
        links += 1
        if links > MAX_HOST_LINKS:
            raise ValueError("host process descriptors exceed scan bound")
        try:
            target = os.readlink(link)
        except (FileNotFoundError, ProcessLookupError):
            return
        if target.startswith("/home/"):
            account, _, suffix = target[6:].partition("/")
            if account in targets and suffix:
                busy.add(account)

    with os.scandir(PROC) as processes:
        for entry in processes:
            if not entry.name.isdecimal():
                continue
            pids += 1
            if pids > MAX_HOST_PIDS:
                raise ValueError("host processes exceed scan bound")
            proc = Path(entry.path)
            try:
                if os.readlink(proc / "ns/pid") != namespace:
                    continue
                check(proc / "cwd")
                with os.scandir(proc / "fd") as descriptors:
                    for fd in descriptors:
                        check(fd.path)
            except (FileNotFoundError, ProcessLookupError):
                continue
            if busy == targets:
                break
    return busy


def clean(accounts, query, pressure, apply, busy_accounts):
    busy = busy_accounts(accounts)
    for account in sorted(busy):
        print(f"nextjs-worktree-prune: preserving {account}; host process in home")
    # Read every running account before removing anything. A failed listing
    # can't silently make newer worktrees appear first in the global order.
    candidates = sorted(c for a in accounts if a not in busy for c in candidates_from(a, query(a, "--list")))
    removed = 0
    for candidate in candidates:
        if not apply:
            print(f"nextjs-worktree-prune: eligible {candidate.account} age_ns={candidate.age_ns}")
            continue
        if not pressure():
            break
        if busy_accounts([candidate.account]):
            print(f"nextjs-worktree-prune: preserving {candidate.account}; host process in home")
            continue
        result = query(candidate.account, "--remove", candidate.token)
        if (not isinstance(result, dict) or result.keys() != {"status"}
                or result["status"] not in ("removed", "skipped", "healthy", "declined")):
            raise ValueError(f"invalid removal result from {candidate.account}")
        print(f"nextjs-worktree-prune: {candidate.account} {result['status']} age_ns={candidate.age_ns}")
        removed += result["status"] == "removed"
    print(f"nextjs-worktree-prune: {len(candidates)} eligible, {removed} removed")
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="remove eligible worktrees while disk is pressured")
    args = parser.parse_args()
    if os.geteuid():
        parser.exit(2, "nextjs-worktree-prune: run the host driver as root\n")
    try:
        # The installed entry point acquires the lock before loading this file.
        if not pressured():
            print("nextjs-worktree-prune: disk has enough space; skipped")
            return
        clean(managed_accounts(), query_worker, pressured, args.apply, host_busy_accounts)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        # Never echo command output, repository paths or container-controlled text.
        parser.exit(1, f"nextjs-worktree-prune: stopped ({type(e).__name__})\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Record root-only Next.js memory snapshots without reading command arguments."""

import argparse
import datetime as dt
import fcntl
import heapq
import json
import os
import re
from pathlib import Path

LOG_DIR = Path("/var/log/nextjs-memory")
LOG_NAME = "samples.jsonl"
MAX_BYTES = 8 * 1024 * 1024
ARCHIVES = 7
TOP_COUNT = 8
MACHINE = re.compile(r"systemd-nspawn@([A-Za-z0-9_-]+)\.service")
HOST_MEMORY = {
    "MemTotal": "mem_total_bytes",
    "MemAvailable": "mem_available_bytes",
    "SwapTotal": "swap_total_bytes",
    "SwapFree": "swap_free_bytes",
}


def fields(path, separator, wanted):
    """Read selected numeric fields, tolerating optional or unavailable counters."""
    try:
        result = {}
        for line in path.read_text().splitlines():
            key, _, raw = line.partition(separator)
            if key in wanted:
                result[key] = int(raw.split()[0])
        return result
    except (OSError, ValueError, IndexError):
        return {}


def integer(path):
    try:
        return int(path.read_text())
    except (OSError, ValueError):
        return None


def pressure(path):
    result = {}
    try:
        for row in path.read_text().splitlines():
            kind, *values = row.split()
            result[kind] = {
                k: int(v) if k == "total" else float(v)
                for k, v in (value.split("=", 1) for value in values)
            }
    except (OSError, ValueError):
        return {}
    return result


def container_memory(cgroup):
    containers = {}
    for folder in sorted((cgroup / "machine.slice").glob("systemd-nspawn@*.service")):
        match = MACHINE.fullmatch(folder.name)
        if not match:
            continue
        row = {
            key: integer(folder / counter)
            for key, counter in (
                ("memory_bytes", "memory.current"),
                ("swap_bytes", "memory.swap.current"),
                ("memory_peak_bytes", "memory.peak"),
                ("swap_peak_bytes", "memory.swap.peak"),
            )
        }
        if row["memory_bytes"] is None:
            continue
        row["stat"] = fields(
            folder / "memory.stat", " ",
            {"anon", "file", "kernel", "shmem", "pgscan_direct", "workingset_refault_file"},
        )
        row["events"] = fields(folder / "memory.events", " ", {"high", "oom", "oom_kill"})
        containers[match[1]] = row
    return containers


def container_name(path):
    try:
        for part in path.read_text().strip().split("/"):
            if match := MACHINE.fullmatch(part):
                return match[1]
    except OSError:
        pass
    return None


def process_memory(proc):
    rows = []
    for folder in proc.iterdir():
        if not folder.name.isascii() or not folder.name.isdecimal():
            continue
        try:
            status = {}
            for line in (folder / "status").read_text(errors="replace").splitlines():
                key, _, value = line.partition(":")
                if key in {"Name", "Uid", "VmRSS", "VmSwap"}:
                    status[key] = value.strip()
            # Kernel threads lack VmRSS. A process may also exit mid-sample.
            if "VmRSS" not in status:
                continue
            rows.append({
                "pid": int(folder.name),
                "name": status["Name"][:64],
                "uid": int(status["Uid"].split()[0]),
                "rss_bytes": int(status["VmRSS"].split()[0]) * 1024,
                "swap_bytes": int(status.get("VmSwap", "0").split()[0]) * 1024,
            })
        except (OSError, ValueError, KeyError, IndexError):
            continue

    top = {}
    for key in ("rss", "swap"):
        measure = key + "_bytes"
        top[key] = heapq.nlargest(
            TOP_COUNT, (row for row in rows if row[measure]),
            key=lambda row: (row[measure], -row["pid"]),
        )
        for row in top[key]:
            if "container" not in row:
                row["container"] = container_name(proc / str(row["pid"]) / "cgroup")
    return top


def collect(proc, cgroup, *, pressure_details=False):
    host = {
        HOST_MEMORY[key]: value * 1024
        for key, value in fields(proc / "meminfo", ":", HOST_MEMORY).items()
    }
    required = set(HOST_MEMORY.values())
    if host.keys() < required or not host["mem_total_bytes"]:
        raise ValueError("incomplete host /proc/meminfo")
    host["swap_used_bytes"] = host["swap_total_bytes"] - host["swap_free_bytes"]
    host["vmstat"] = fields(proc / "vmstat", " ", {"pswpin", "pswpout", "pgmajfault", "oom_kill"})
    host["memory_pressure"] = pressure(proc / "pressure/memory")
    host["io_pressure"] = pressure(proc / "pressure/io")
    mem_low = host["mem_available_bytes"] * 5 <= host["mem_total_bytes"]
    swap_high = host["swap_total_bytes"] > 0 and host["swap_used_bytes"] * 4 >= host["swap_total_bytes"] * 3
    stalled = host["memory_pressure"].get("some", {}).get("avg10", 0) >= 5
    try:
        boot_id = (proc / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = None
    record = {
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "boot_id": boot_id,
        "host": host,
        "containers": container_memory(cgroup),
        "under_pressure": mem_low or swap_high or stalled,
    }
    if pressure_details or record["under_pressure"]:
        record["top"] = process_memory(proc)
    return record


def valid_log_size(path):
    """Remove a crash-truncated final line before it can enter an archive."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    try:
        size = os.fstat(fd).st_size
        if not size or os.pread(fd, 1, size - 1) == b"\n":
            return size
        end = size
        while end:
            start = max(0, end - 4096)
            boundary = os.pread(fd, end - start, start).rfind(b"\n")
            if boundary >= 0:
                end = start + boundary + 1
                break
            end = start
        os.ftruncate(fd, end)
        return end
    finally:
        os.close(fd)


def append_record(directory, sample, *, max_bytes=MAX_BYTES, archives=ARCHIVES):
    """Serialize rotation and keep no more than archives+1 bounded private files."""
    data = json.dumps(sample, separators=(",", ":")).encode() + b"\n"
    if max_bytes < len(data) or archives < 1:
        raise ValueError("invalid rotation settings or oversized sample")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = directory / LOG_NAME
    with os.fdopen(os.open(directory / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        size = valid_log_size(current)
        rotated = size > 0 and size + len(data) > max_bytes
        if rotated:
            for index in range(archives, 0, -1):
                old = current if index == 1 else directory / f"{LOG_NAME}.{index - 1}"
                if old.exists():
                    old.replace(directory / f"{LOG_NAME}.{index}")
        new_file = rotated or not current.exists()
        fd = os.open(current, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as log:
            log.write(data)
            log.flush()
            os.fdatasync(fd)
        if new_file:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print a sample instead of writing the log")
    args = parser.parse_args()
    sample = collect(Path("/proc"), Path("/sys/fs/cgroup"), pressure_details=True)
    if args.stdout:
        print(json.dumps(sample, separators=(",", ":")))
    else:
        append_record(LOG_DIR, sample)


if __name__ == "__main__":
    main()
